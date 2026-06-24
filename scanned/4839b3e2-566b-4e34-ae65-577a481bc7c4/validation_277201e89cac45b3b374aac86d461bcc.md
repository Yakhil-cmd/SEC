### Title
VIP Caller Starvation via Slot Exhaustion in `try_borrow_slot` — (`rs/nervous_system/clients/src/management_canister_client.rs`)

---

### Summary

`LimitedOutstandingCallsManagementCanisterClient::try_borrow_slot` contains a logic error: the `if *available_slot_count == 0` guard fires for **all** callers — including VIPs — before the VIP's `used_slot_count = 0` branch is ever reached. An unprivileged attacker who floods NNS Root's public `canister_status` endpoint with 167 concurrent calls can exhaust all slots, causing subsequent NNS Governance calls (`update_canister_settings`, `stop_canister`, etc.) to receive `SysTransient` errors.

---

### Finding Description

**The bug — `try_borrow_slot`:** [1](#0-0) 

```rust
fn try_borrow_slot(&self) -> Result<SlotLoan, (i32, String)> {
    let used_slot_count = if self.is_caller_vip { 0 } else { 1 };  // VIPs use 0 slots

    self.available_slot_count
        .with_borrow_mut(|available_slot_count| {
            if *available_slot_count == 0 {          // ← fires for VIPs too
                let code = RejectCode::SysTransient as i32;
                let message = "Unavailable. Maybe, try again later?".to_string();
                return Err((code, message));         // ← VIP is rejected here
            }
            *available_slot_count = available_slot_count.saturating_sub(used_slot_count);
            // VIPs would subtract 0 — but they never reach this line when count == 0
            Ok(())
        })?;
    ...
}
```

The intent is that VIPs (NNS canisters) never consume slots (`used_slot_count = 0`), so they should never be blocked. But the early-exit `== 0` check does not distinguish VIP from non-VIP. When the counter reaches exactly 0, VIPs are rejected identically to non-VIPs.

**The open attack surface — `canister_status` on NNS Root:** [2](#0-1) 

The `canister_status` endpoint is a public `#[update]` method with **no access control**. The comment explicitly states "anyone can get the status of any NNS canister." There is no `check_caller_is_governance()` or equivalent guard.

**Slot count initialization:** [3](#0-2) 

`AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT` is initialized to exactly 167.

**VIP determination:** [4](#0-3) 

VIP status is granted only to NNS canisters. Any other caller (user, external canister) is non-VIP and consumes one slot per call.

**Governance-only operations that use the same slot pool:** [5](#0-4) [6](#0-5) 

`update_canister_settings`, `stop_canister`, `change_canister_controllers`, `take_canister_snapshot`, and `load_canister_snapshot` all call `new_management_canister_client()` → `try_borrow_slot()` and share the same `AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT` thread-local.

---

### Impact Explanation

When all 167 slots are held by non-VIP calls, NNS Governance's calls to NNS Root for `update_canister_settings`, `stop_canister`, etc. receive `Err((SysTransient, "Unavailable. Maybe, try again later?"))`. These are the exact calls used to execute NNS proposals for canister upgrades, controller changes, and emergency stops. A sustained attack can indefinitely delay or stall critical NNS governance operations, including security patches and recovery actions.

---

### Likelihood Explanation

The attack requires:
1. A canister on the IC (accessible to anyone with cycles).
2. Sending 167 concurrent inter-canister calls to NNS Root's `canister_status` in a single execution round — all targeting any NNS-Root-controlled canister (e.g., the Governance canister itself).
3. Repeating each round to maintain exhaustion before the previous batch of management canister responses returns.

The management canister responds within 1–2 rounds (~1–2 seconds). The attacker must sustain the flood continuously, which is cycle-expensive but not technically infeasible. The attack window per batch is narrow but repeatable and automatable.

---

### Recommendation

In `try_borrow_slot`, skip the `== 0` rejection for VIP callers. The corrected logic:

```rust
fn try_borrow_slot(&self) -> Result<SlotLoan, (i32, String)> {
    let used_slot_count = if self.is_caller_vip { 0 } else { 1 };

    self.available_slot_count
        .with_borrow_mut(|available_slot_count| {
            if *available_slot_count == 0 && !self.is_caller_vip {
                let code = RejectCode::SysTransient as i32;
                let message = "Unavailable. Maybe, try again later?".to_string();
                return Err((code, message));
            }
            *available_slot_count = available_slot_count.saturating_sub(used_slot_count);
            Ok(())
        })?;
    ...
}
```

This ensures VIP callers always pass through regardless of the slot counter state, while non-VIP callers are still rate-limited.

---

### Proof of Concept

State-machine test outline:
1. Deploy NNS Root with `AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT = 167`.
2. Deploy an attacker canister (non-NNS principal).
3. From the attacker canister, send 167 concurrent inter-canister calls to NNS Root's `canister_status` targeting a slow-responding or any NNS-controlled canister.
4. Before any response returns, send a `update_canister_settings` call from NNS Governance (VIP).
5. Assert the Governance call returns `Err((SysTransient, "Unavailable..."))`.
6. Assert that after the 167 attacker calls complete, the slot counter recovers to 167 and a subsequent Governance call succeeds.

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

**File:** rs/nns/handlers/root/impl/canister/canister.rs (L220-230)
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
```

**File:** rs/nns/handlers/root/impl/canister/canister.rs (L326-332)
```rust

```
