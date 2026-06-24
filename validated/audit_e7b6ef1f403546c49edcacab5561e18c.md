Audit Report

## Title
VIP Bypass Incomplete in `try_borrow_slot`: Unprivileged Callers Can Exhaust Management Canister Call Slots and Block Governance Operations — (`rs/nervous_system/clients/src/management_canister_client.rs`)

## Summary
`LimitedOutstandingCallsManagementCanisterClient::try_borrow_slot` intends to give VIP callers (NNS canisters, including governance) unlimited access to management canister call slots by setting `used_slot_count = 0`. However, the zero-slot guard (`if *available_slot_count == 0 { return Err(...) }`) executes unconditionally before the subtraction and is not conditioned on `is_caller_vip`. An unprivileged external caller can exhaust all 167 slots via concurrent `canister_status` calls, causing governance-initiated operations (`update_canister_settings`, `take_canister_snapshot`, `load_canister_snapshot`) to fail with `SysTransient` for the duration of the attack.

## Finding Description
In `rs/nervous_system/clients/src/management_canister_client.rs` at lines 264–287, `try_borrow_slot` computes `used_slot_count = 0` for VIP callers but then unconditionally enters the `with_borrow_mut` closure where the guard `if *available_slot_count == 0 { return Err(...) }` fires for all callers regardless of VIP status:

```rust
fn try_borrow_slot(&self) -> Result<SlotLoan, (i32, String)> {
    let used_slot_count = if self.is_caller_vip { 0 } else { 1 };  // L265

    self.available_slot_count
        .with_borrow_mut(|available_slot_count| {
            if *available_slot_count == 0 {          // L269 — no VIP exemption
                let code = RejectCode::SysTransient as i32;
                return Err((code, message));
            }
            *available_slot_count = available_slot_count.saturating_sub(used_slot_count);
            Ok(())
        })?;
``` [1](#0-0) 

The public `canister_status` endpoint in `rs/nns/handlers/root/impl/canister/canister.rs` carries no caller restriction and calls `new_management_canister_client()`, which sets `is_caller_vip = false` for any non-NNS principal: [2](#0-1) 

Each non-VIP call decrements `AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT` (initialized to 167) by 1 and holds the `SlotLoan` alive across the `await` point until the management canister responds. Once all 167 slots are consumed, `available_slot_count == 0`, and the next call to `try_borrow_slot` — even from governance (VIP) — hits the guard at L269 and returns `Err((SysTransient, "Unavailable. Maybe, try again later?"))`. [3](#0-2) 

Governance-only endpoints `update_canister_settings`, `take_canister_snapshot`, and `load_canister_snapshot` all call `new_management_canister_client()` and thus all route through `try_borrow_slot`: [4](#0-3) [5](#0-4) [6](#0-5) 

The `is_caller_vip` check in `new_management_canister_client` correctly identifies NNS canisters, but the protection it is supposed to confer is never actually applied in the zero-slot path: [7](#0-6) 

## Impact Explanation
An attacker can transiently block NNS governance execution of `update_canister_settings`, `take_canister_snapshot`, and `load_canister_snapshot` proposal types. Governance proposals that execute these operations via the NNS root canister will receive `SysTransient` rejections for as long as the attacker maintains 167 in-flight `canister_status` calls. This constitutes an application/platform-level DoS on NNS governance operations — a **High** severity impact matching "Application/platform-level DoS … or subnet availability impact not based on raw volumetric DDoS."

## Likelihood Explanation
The attack requires sending 167 concurrent ingress messages to the NNS root canister, well within the IC ingress queue limit of 500. No privileged access, key material, or majority corruption is required — any user principal suffices. Sustaining the attack requires targeting canisters that respond slowly (e.g., stopped canisters), which is achievable by any attacker. The attack is repeatable and can be continuously re-flooded to sustain the denial.

## Recommendation
Fix `try_borrow_slot` to skip the zero-slot guard entirely for VIP callers:

```rust
fn try_borrow_slot(&self) -> Result<SlotLoan, (i32, String)> {
    if !self.is_caller_vip {
        self.available_slot_count
            .with_borrow_mut(|available_slot_count| {
                if *available_slot_count == 0 {
                    let code = RejectCode::SysTransient as i32;
                    return Err((code, "Unavailable. Maybe, try again later?".to_string()));
                }
                *available_slot_count = available_slot_count.saturating_sub(1);
                Ok(())
            })?;
    }

    Ok(SlotLoan {
        available_slot_count: self.available_slot_count,
        used_slot_count: if self.is_caller_vip { 0 } else { 1 },
    })
}
```

This ensures VIP callers always succeed regardless of slot availability, matching the stated design intent.

## Proof of Concept
State-machine test outline:
1. Send 167 `canister_status` ingress calls from a non-NNS principal targeting a stopped canister (so management canister responses are delayed).
2. Advance the state machine to process all 167 calls up to their `await` point but not to completion.
3. Assert `AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT == 0`.
4. Send an `update_canister_settings` call from the NNS governance canister principal.
5. Assert the response is `Err` with `SysTransient` rejection code.
6. Allow the 167 `canister_status` calls to complete.
7. Assert `AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT == 167` (slots restored via `SlotLoan` drop).
8. Retry `update_canister_settings` from governance and assert it succeeds.

### Citations

**File:** rs/nervous_system/clients/src/management_canister_client.rs (L264-287)
```rust
    fn try_borrow_slot(&self) -> Result<SlotLoan, (i32, String)> {
        let used_slot_count = if self.is_caller_vip { 0 } else { 1 };

        self.available_slot_count
            .with_borrow_mut(|available_slot_count| {
                if *available_slot_count == 0 {
                    // This is somewhat of a lie, but is the best fit.
                    let code = RejectCode::SysTransient as i32;

                    let message = "Unavailable. Maybe, try again later?".to_string();

                    return Err((code, message));
                }

                *available_slot_count = available_slot_count.saturating_sub(used_slot_count);
                Ok(())
            })?;

        let available_slot_count = self.available_slot_count;
        Ok(SlotLoan {
            available_slot_count,
            used_slot_count,
        })
    }
```

**File:** rs/nns/handlers/root/impl/canister/canister.rs (L47-51)
```rust
thread_local! {
    // How this value was chosen: queues become full at 500. This is 1/3 of that, which seems to be
    // a reasonable balance.
    static AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT: RefCell<u64> = const { RefCell::new(167) };
}
```

**File:** rs/nns/handlers/root/impl/canister/canister.rs (L57-66)
```rust
    // Here, VIP = is an NNS canister
    let is_caller_vip = CanisterId::try_from(caller())
        .map(|caller| ALL_NNS_CANISTER_IDS.contains(&&caller))
        .unwrap_or(false);

    LimitedOutstandingCallsManagementCanisterClient::new(
        client,
        &AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT,
        is_caller_vip,
    )
```

**File:** rs/nns/handlers/root/impl/canister/canister.rs (L88-98)
```rust
#[update]
async fn canister_status(canister_id_record: CanisterIdRecord) -> CanisterStatusResult {
    let client = new_management_canister_client();

    let canister_status_response = client
        .canister_status(canister_id_record)
        .await
        .map(CanisterStatusResult::from);

    canister_status_response.unwrap()
}
```

**File:** rs/nns/handlers/root/impl/canister/canister.rs (L221-230)
```rust
async fn update_canister_settings(
    update_settings: UpdateCanisterSettingsRequest,
) -> UpdateCanisterSettingsResponse {
    check_caller_is_governance();
    canister_management::update_canister_settings(
        update_settings,
        &mut new_management_canister_client(),
    )
    .await
}
```

**File:** rs/nns/handlers/root/impl/canister/canister.rs (L245-254)
```rust
async fn take_canister_snapshot(
    take_canister_snapshot_request: TakeCanisterSnapshotRequest,
) -> TakeCanisterSnapshotResponse {
    check_caller_is_governance();
    ic_nervous_system_root::take_canister_snapshot::take_canister_snapshot(
        take_canister_snapshot_request,
        new_management_canister_client(),
    )
    .await
}
```

**File:** rs/nns/handlers/root/impl/canister/canister.rs (L259-268)
```rust
async fn load_canister_snapshot(
    load_canister_snapshot_request: LoadCanisterSnapshotRequest,
) -> LoadCanisterSnapshotResponse {
    check_caller_is_governance();
    ic_nervous_system_root::load_canister_snapshot::load_canister_snapshot(
        load_canister_snapshot_request,
        new_management_canister_client(),
    )
    .await
}
```
