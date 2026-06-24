All three files confirm the claim. Let me verify one additional detail about the `change_canister` path to assess recovery.

Audit Report

## Title
Permanent Slot Exhaustion via Unauthenticated `canister_status` Trap — (`rs/nns/handlers/root/impl/canister/canister.rs`)

## Summary

The public `canister_status` endpoint on NNS Root traps on any management-canister rejection via `.unwrap()`. Because Wasm traps do not unwind the stack, the `SlotLoan` RAII guard that restores `AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT` is never dropped. The slot decrement occurs in the pre-await phase (committed to canister state before the callback runs) and is not rolled back when the callback traps. An unprivileged attacker can permanently exhaust all 167 slots with 167 sequential update calls, blocking all management-canister operations routed through NNS Root — including those from NNS Governance.

## Finding Description

**Entry point:** `canister_status` at line 88 of `rs/nns/handlers/root/impl/canister/canister.rs` is a `#[update]` endpoint with no caller authentication. [1](#0-0) 

**Slot accounting:** `new_management_canister_client()` wraps the real client in `LimitedOutstandingCallsManagementCanisterClient`. `try_borrow_slot()` decrements `AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT` (initialized to 167) and returns a `SlotLoan` RAII guard. [2](#0-1) 

**Slot borrow in the async method:** The slot is decremented and `_loan` is created *before* the `.await` on the inner call. The IC commits canister state (including the decremented count) when the inter-canister call is sent. [3](#0-2) 

**The trap:** Back in `canister_status`, the result of the management canister call is passed through `.map(CanisterStatusResult::from)` and then `.unwrap()`. When the management canister rejects (e.g., NNS Root is not a controller of the target canister), `.unwrap()` on `Err(...)` causes a Wasm trap. [4](#0-3) 

**RAII guard never runs:** Wasm traps do not unwind the stack. `SlotLoan::drop()` — the only code path that increments `available_slot_count` back — is never invoked. [5](#0-4) 

**VIP bypass is ineffective:** The VIP path (`is_caller_vip = true`, `used_slot_count = 0`) does not bypass the `if *available_slot_count == 0` guard. When the count reaches zero, `try_borrow_slot()` returns `Err` for every caller, including NNS Governance. [6](#0-5) 

**Acknowledged design:** The codebase explicitly documents that NNS Root's `canister_status` uses trapping as its error-propagation mechanism, confirming this is the production behavior. [7](#0-6) 

**No self-recovery:** Once the count is 0, all calls to `try_borrow_slot()` return `Err` before any inter-canister call is made, so no `SlotLoan` is ever created and no drop ever fires. The count is permanently stuck at 0 until the canister is upgraded (which resets the `thread_local!`). [8](#0-7) 

## Impact Explanation

Once all 167 slots are exhausted, every call to `try_borrow_slot()` returns `Err((SysTransient, "Unavailable. Maybe, try again later?"))`. This blocks all management-canister operations routed through NNS Root that use `new_management_canister_client()`: `update_canister_settings`, `take_canister_snapshot`, `load_canister_snapshot`, and `change_canister_controllers`. NNS Governance relies on these operations for canister lifecycle management. This is a concrete, application/platform-level DoS on NNS governance infrastructure — matching the **High ($2,000–$10,000)** impact class: *Application/platform-level DoS or subnet availability impact not based on raw volumetric DDoS*, and *Significant NNS security impact with concrete user or protocol harm*. [9](#0-8) 

## Likelihood Explanation

- No authentication required; any principal including anonymous can call `canister_status`
- No special cycles cost beyond normal update-call fees
- The management canister deterministically rejects `canister_status` for any canister NNS Root does not control
- 167 sequential calls suffice; no concurrency or race conditions required
- The trap is deterministic and reproducible [10](#0-9) 

## Recommendation

Change `canister_status` to return `Result<CanisterStatusResult, String>` and propagate errors without trapping. This ensures `SlotLoan` is always dropped normally (restoring the slot count) regardless of whether the management canister accepts or rejects the call:

```rust
#[update]
async fn canister_status(canister_id_record: CanisterIdRecord) -> Result<CanisterStatusResult, String> {
    let client = new_management_canister_client();
    client
        .canister_status(canister_id_record)
        .await
        .map(CanisterStatusResult::from)
        .map_err(|(_, msg)| msg)
}
``` [3](#0-2) 

## Proof of Concept

```rust
// State-machine / PocketIC integration test:
// 1. Install NNS with Root canister.
// 2. Pick any canister ID not controlled by Root (e.g., CanisterId::from_u64(9999)).
// 3. Call canister_status on Root from anonymous principal 167 times.
//    Each call returns a replica-level error (trap); verify via Err response.
// 4. Confirm AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT == 0
//    (observable via /metrics or by attempting a VIP call and receiving
//    SysTransient / "Unavailable. Maybe, try again later?").
// 5. Have NNS Governance call update_canister_settings or take_canister_snapshot
//    through Root. Assert it fails with SysTransient / "Unavailable".
// 6. Confirm no non-upgrade recovery path exists.
``` [11](#0-10)

### Citations

**File:** rs/nns/handlers/root/impl/canister/canister.rs (L47-67)
```rust
thread_local! {
    // How this value was chosen: queues become full at 500. This is 1/3 of that, which seems to be
    // a reasonable balance.
    static AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT: RefCell<u64> = const { RefCell::new(167) };
}

fn new_management_canister_client() -> impl ManagementCanisterClient {
    let client =
        ManagementCanisterClientImpl::<CdkRuntime>::new(Some(&PROXIED_CANISTER_CALLS_TRACKER));

    // Here, VIP = is an NNS canister
    let is_caller_vip = CanisterId::try_from(caller())
        .map(|caller| ALL_NNS_CANISTER_IDS.contains(&&caller))
        .unwrap_or(false);

    LimitedOutstandingCallsManagementCanisterClient::new(
        client,
        &AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT,
        is_caller_vip,
    )
}
```

**File:** rs/nns/handlers/root/impl/canister/canister.rs (L81-98)
```rust
/// Returns the status of the canister specified in the input.
///
/// The status of NNS canisters should be public information: anyone can get the
/// status of any NNS canister.
///
/// This must be an update, not a query, because an inter-canister call to the
/// management canister is required.
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

**File:** rs/nns/handlers/root/impl/canister/canister.rs (L220-268)
```rust
#[update]
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

/// Creates a new canister on the specified subnet and installs code into it.
/// Only callable by NNS Governance.
#[update]
async fn create_canister_and_install_code(
    request: CreateCanisterAndInstallCodeRequest,
) -> CreateCanisterAndInstallCodeResponse {
    check_caller_is_governance();
    canister_management::create_canister_and_install_code(request).await
}

/// Takes a snapshot of a canister controlled by NNS Root. Only callable by NNS
/// Governance.
#[update]
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

/// Loads a snapshot of a canister controlled by NNS Root. Only callable by NNS
/// Governance.
#[update]
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

**File:** rs/nervous_system/clients/src/management_canister_client.rs (L295-301)
```rust
    async fn canister_status(
        &self,
        canister_id_record: CanisterIdRecord,
    ) -> Result<CanisterStatusResultFromManagementCanister, (i32, String)> {
        let _loan = self.try_borrow_slot()?;
        self.inner.canister_status(canister_id_record).await
    }
```

**File:** rs/nervous_system/clients/src/management_canister_client.rs (L365-372)
```rust
impl Drop for SlotLoan {
    fn drop(&mut self) {
        self.available_slot_count
            .with_borrow_mut(|available_slot_count| {
                *available_slot_count = available_slot_count.saturating_add(self.used_slot_count);
            });
    }
}
```

**File:** rs/nns/handlers/root/interface/src/client.rs (L246-254)
```rust
    // There is no `err_canister_status_from_root` because the NNS root's canister_status makes
    // use of the canister trap to propagate errors, therefore all errors come via the
    // replica level error
    pub fn err_canister_status_from_replica(
        code: Option<i32>,
        description: String,
    ) -> SpyNnsRootCanisterClientReply {
        SpyNnsRootCanisterClientReply::CanisterStatus(Err((code, description)))
    }
```
