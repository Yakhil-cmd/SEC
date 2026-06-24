Let me look at the critical `.unwrap()` call and the IC state rollback behavior more carefully.

Now I have all the evidence needed. Let me synthesize the complete picture.

**Key facts established:**

1. `canister_status` in `canister.rs` line 97 calls `.unwrap()` on the result — no access control guard, publicly callable.
2. `LimitedOutstandingCallsManagementCanisterClient::canister_status` decrements `available_slot_count` **before** the await, then holds `_loan` across the await.
3. The IC execution model (confirmed by `system_api.rs` lines 1799–1813): when a `ReplyCallback`/`RejectCallback` traps, **all heap changes from that callback execution are discarded** — only log changes survive.
4. The management canister rejects `canister_status` if the caller (NNS Root) is not a controller of the target canister (confirmed by multiple tests).

**Exact execution trace for the exploit:**

- **Phase 1 (update message):** `try_borrow_slot()` decrements `available_slot_count` N→N-1. Inter-canister call registered. **State committed at N-1.**
- **Phase 2 (reject callback):** Management canister rejects (NNS Root not a controller). `_loan` is dropped → `available_slot_count` N-1→N (heap write). Then `.unwrap()` on `Err(...)` panics → Wasm trap → **entire callback heap rolled back to N-1**. The `_loan` drop is undone. Slot permanently leaked.

Repeat 167 times → `available_slot_count` = 0 → all non-VIP calls return `SysTransient` error permanently.

---

### Title
Slot Leak via Wasm Trap Rollback in `canister_status` Causes Permanent DoS of NNS Root Management-Canister Proxying — (`rs/nns/handlers/root/impl/canister/canister.rs`)

### Summary
The publicly-accessible `canister_status` update method on NNS Root calls `.unwrap()` on the management canister response after the async await. When the management canister rejects the call (because NNS Root is not a controller of the queried canister), `.unwrap()` panics, producing a Wasm trap. The IC execution model rolls back all heap changes from the callback execution — including the `available_slot_count` increment performed by `SlotLoan::drop` — permanently leaking the slot. An unprivileged attacker can repeat this 167 times to exhaust all slots.

### Finding Description

`LimitedOutstandingCallsManagementCanisterClient::try_borrow_slot` decrements `available_slot_count` before the first await point and stores a `SlotLoan` in the future's heap state to return the slot on drop: [1](#0-0) 

The `SlotLoan::drop` increments `available_slot_count` back: [2](#0-1) 

The outer `canister_status` handler in NNS Root is publicly accessible (no access control) and calls `.unwrap()` on the result after the await: [3](#0-2) 

The IC execution model, confirmed in `system_api.rs`, discards all heap state changes when a `ReplyCallback` or `RejectCallback` traps — only log changes survive: [4](#0-3) 

The slot pool is initialized at 167: [5](#0-4) 

**Execution trace:**
1. Attacker calls `canister_status` on NNS Root with a canister ID NNS Root does not control.
2. `try_borrow_slot()` decrements `available_slot_count` from N to N-1. State committed.
3. Management canister rejects the call (`CanisterInvalidController`).
4. Reject callback fires. `_loan` is dropped → `available_slot_count` incremented to N (heap write). Then `.unwrap()` on `Err(...)` panics → Wasm trap.
5. IC rolls back all callback-phase heap changes. `available_slot_count` reverts to N-1. Slot permanently leaked.

The management canister enforces controller-only access to `canister_status`, confirmed by tests: [6](#0-5) 

### Impact Explanation
After 167 such calls, `available_slot_count` reaches 0. Every subsequent non-VIP call to any management-canister-proxying method returns `SysTransient` ("Unavailable. Maybe, try again later?"). VIP callers (NNS canisters) are unaffected because `used_slot_count = 0` for them. Recovery requires upgrading NNS Root via a governance proposal, which takes days. During this window, any external caller (e.g., SNS-W via `change_canister_controllers`, or any user calling `canister_status`) is permanently blocked.

### Likelihood Explanation
The attack requires only 167 ordinary update calls to a publicly-accessible NNS Root endpoint with any canister ID the attacker does not share control of with NNS Root. No special privileges, no cycles beyond call fees, no coordination. The trigger condition (management canister rejecting a non-controller call) is deterministic and reliable.

### Recommendation
1. **Immediate fix**: Replace `.unwrap()` on line 97 of `canister.rs` with proper error propagation (return `Err` or a typed error response) so the callback never traps.
2. **Structural fix**: Move `SlotLoan` drop to a `call_on_cleanup` closure registered via `ic0::call_on_cleanup`, which the IC executes even when the main callback traps and whose state changes **are** committed. This makes slot return unconditional regardless of trap behavior.
3. **Defense in depth**: Audit all other callers of `try_borrow_slot` for similar post-await panic paths.

### Proof of Concept
```
for i in 1..=167:
    call NNS Root `canister_status` with canister_id = <any canister NNS Root does not control>
    # management canister rejects → .unwrap() traps → slot leaked

# Now available_slot_count == 0
call NNS Root `canister_status` with any NNS canister ID
# Returns: Err(SysTransient, "Unavailable. Maybe, try again later?")
# DoS confirmed
```

Assert: `AVAILABLE_MANAGEMENT_CANISTER_CALL_SLOT_COUNT` before = 167, after = 0, and does not recover without canister upgrade.

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

**File:** rs/embedders/src/wasmtime_embedder/system_api.rs (L1795-1813)
```rust
            ApiType::SystemTask { time, .. }
            | ApiType::Update { time, .. }
            | ApiType::Cleanup { time, .. }
            | ApiType::ReplyCallback { time, .. }
            | ApiType::RejectCallback { time, .. } => match &self.execution_error {
                Some(err) => {
                    self.add_canister_log_for_trap(err, time, &mut system_state_modifications);
                    SystemStateModifications {
                        new_certified_data: None,
                        cycles_balance_change: CyclesBalanceChange::zero(),
                        reserved_cycles: Cycles::zero(),
                        consumed_cycles_by_use_case: ConsumedCyclesDuringExecution::default(),
                        call_context_balance_taken: None,
                        request_slots_used: BTreeMap::new(),
                        requests: vec![],
                        new_global_timer: None,
                        canister_log: system_state_modifications.canister_log,
                        should_bump_canister_version: false,
                    }
```

**File:** rs/execution_environment/src/canister_manager/tests.rs (L1377-1399)
```rust
#[test]
fn get_canister_status_with_incorrect_controller_fails() {
    let mut test = ExecutionTestBuilder::new().build();

    let canister_id = test.create_canister(*INITIAL_CYCLES);

    // Switch the user so the canister_status request comes from a non-controller.
    let test_user = user_test_id(42);
    test.set_user_id(test_user);
    assert!(
        !test
            .canister_state(canister_id)
            .controllers()
            .contains(test_user.get_ref())
    );

    let err = test.canister_status(canister_id).unwrap_err();

    assert_eq!(err.code(), ErrorCode::CanisterInvalidController);
    assert!(err.description().contains(&format!(
        "Only the controllers of the canister {canister_id} can control it"
    )));
}
```
