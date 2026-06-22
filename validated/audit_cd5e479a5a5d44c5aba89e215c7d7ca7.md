### Title
Wasm Trap in `canister_status` Callback Rolls Back `SlotLoan::drop`, Permanently Leaking Slots — (`rs/nns/handlers/root/impl/canister/canister.rs`)

---

### Summary

The NNS root canister's public `canister_status` update endpoint calls `.unwrap()` on the management canister result **after** the `SlotLoan` RAII guard has already been dropped within the same callback execution. Because the IC rolls back all heap mutations when a callback traps, the slot restoration performed by `SlotLoan::drop` is undone. An unprivileged caller can exhaust all 167 slots by repeatedly triggering this trap, permanently disabling the management canister call capacity for non-VIP callers.

---

### Finding Description

**Entry point — no access control:** [1](#0-0) 

The `canister_status` endpoint is a public `#[update]` with no caller restriction. Any principal can invoke it with an arbitrary `CanisterIdRecord`.

**Slot borrow and RAII guard:** [2](#0-1) 

`try_borrow_slot` decrements `available_slot_count` and returns a `SlotLoan`. This decrement happens in **Message 1** (the initial call execution, before the first `await`). The heap is committed with the decremented value.

**`SlotLoan::drop` restores the slot:** [3](#0-2) 

**The trap site — `.unwrap()` after the await:** [4](#0-3) 

**Execution sequence in Message 2 (callback):**

```
[heap at start of callback: available_slot_count = N-1]
  1. LimitedOutstandingCallsManagementCanisterClient::canister_status resumes
  2. inner.canister_status returns Err(...)
  3. _loan is dropped → available_slot_count incremented to N  ← heap mutation
  4. Err propagates back to canister.rs::canister_status
  5. .map(CanisterStatusResult::from) → still Err
  6. .unwrap() panics → Wasm TRAP
  7. IC rolls back Message 2 heap → available_slot_count reverts to N-1
```

The IC specification mandates that when a response callback traps, the canister's state is rolled back to the state at the beginning of that callback. Step 3's increment is undone by step 7. The slot is permanently leaked.

**Slot capacity:** [5](#0-4) 

167 slots are available. After 167 leaked slots, `available_slot_count` reaches 0.

**Exhaustion check:** [6](#0-5) 

Once `available_slot_count == 0`, all non-VIP calls return `Err(SysTransient, "Unavailable")`.

---

### Impact Explanation

After 167 trapped callbacks, every non-VIP call to any `LimitedOutstandingCallsManagementCanisterClient` method on the NNS root canister returns a `SysTransient` error immediately. VIP callers (NNS canisters) are unaffected because `used_slot_count = 0` for them. [7](#0-6) 

The public `canister_status` endpoint becomes permanently non-functional for all non-NNS principals until the canister is upgraded or restarted.

---

### Likelihood Explanation

- The `canister_status` endpoint is public with no access control.
- Triggering a management canister rejection is trivial: pass any canister ID that NNS root does not control (e.g., any user-owned canister).
- 167 update calls are sufficient to exhaust all slots. Each call costs only ingress fees.
- No privileged access, key material, or subnet-majority corruption is required.

---

### Recommendation

Replace the `.unwrap()` with proper error propagation so the endpoint returns an error to the caller instead of trapping:

```rust
// Instead of:
canister_status_response.unwrap()

// Use:
canister_status_response.map_err(|(code, msg)| trap(&msg))
// or return a Result and let the CDK encode the rejection
```

Alternatively, move the `.unwrap()` inside the `LimitedOutstandingCallsManagementCanisterClient` scope so that any panic occurs before `_loan` is dropped — but this is architecturally worse. The correct fix is to never trap in a callback that holds borrowed resources.

---

### Proof of Concept

1. Obtain any canister ID that NNS root does not control (e.g., a freshly created user canister).
2. Send 167 update calls to NNS root's `canister_status` with that canister ID.
3. Each call: management canister rejects → `Err` returned → `.unwrap()` traps → callback rolled back → slot leaked.
4. On the 168th call from any non-VIP principal to any management canister method, observe `Err(SysTransient, "Unavailable. Maybe, try again later?")` returned immediately without making any inter-canister call.

### Citations

**File:** rs/nns/handlers/root/impl/canister/canister.rs (L47-51)
```rust
thread_local! {
    // How this value was chosen: queues become full at 500. This is 1/3 of that, which seems to be
    // a reasonable balance.
    static AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT: RefCell<u64> = const { RefCell::new(167) };
}
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
