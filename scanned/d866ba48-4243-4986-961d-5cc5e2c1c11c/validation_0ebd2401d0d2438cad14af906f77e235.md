### Title
`CanisterManager::uninstall_code` Allows Uninstalling Empty Canisters, Injecting Spurious `CanisterCodeUninstall` Entries into Certified Canister History - (File: `rs/execution_environment/src/canister_manager.rs`)

---

### Summary

`CanisterManager::uninstall_code` does not check whether a canister has code installed (`execution_state.is_some()`) before proceeding. When called on an empty canister, it unconditionally appends a `CanisterCodeUninstall` change record to the certified canister history and bumps the canister version, even though no code was ever present. This desynchronizes the certified on-chain history from the actual canister state and can mislead any consumer of the `canister_info` endpoint.

---

### Finding Description

`CanisterManager::uninstall_code` in `rs/execution_environment/src/canister_manager.rs` performs two unconditional state mutations regardless of whether the canister has an `execution_state`:

1. It calls `uninstall_canister(...)`, which always calls `canister.system_state.bump_canister_version()`.
2. It calls `canister.add_canister_change(time, origin, CanisterChangeDetails::CanisterCodeUninstall)`, which always appends a `CanisterCodeUninstall` record to the certified canister history and increments `total_num_changes`.

Neither `uninstall_code` nor `uninstall_canister` contains any guard of the form `if canister.execution_state.is_none() { return Err(...) }`. The existing test `uninstall_code_on_empty_canister` explicitly confirms this: it creates a canister with no module (`module_hash` is `None`), calls `uninstall_code`, and asserts success with no error.

The `canister_info` management-canister method reads directly from this history and returns it as part of the certified replicated state, queryable by any canister on the IC. [1](#0-0) [2](#0-1) [3](#0-2) 

---

### Impact Explanation

The certified canister history (exposed via `canister_info`) is part of the IC's replicated, certified state tree. A canister controller can call `UninstallCode` on their own empty canister an arbitrary number of times, each call:

- Appending a false `CanisterCodeUninstall` record to the certified history, incrementing `total_num_changes`.
- Bumping the canister version without any actual code change.
- Consuming subnet memory for each spurious history entry (confirmed by `uninstall_code_on_empty_canister_updates_subnet_available_memory`).

Any canister or off-chain indexer that calls `canister_info` and relies on the certified history to reconstruct a canister's code-deployment lifecycle will receive a certified but false record. The `module_hash` field in the `canister_info` response will correctly show `None`, but the `recent_changes` list will contain `code_uninstall` entries that never corresponded to any actual code removal, desynchronizing the certified history from the true installation set. [4](#0-3) [5](#0-4) 

---

### Likelihood Explanation

Any canister controller — including an unprivileged user who created a canister and never installed code — can trigger this via a standard ingress message to `ic:00` with method `UninstallCode`. No special privilege, governance majority, or threshold key is required. The entry path is the same as any normal management-canister call. [6](#0-5) 

---

### Recommendation

Add a guard at the top of `uninstall_code` (or at the start of `uninstall_canister`) that returns an error if the canister has no execution state:

```rust
pub(crate) fn uninstall_code(
    &self,
    origin: CanisterChangeOrigin,
    canister: &mut CanisterState,
    round_limits: &mut RoundLimits,
    subnet_admins: Option<BTreeSet<PrincipalId>>,
    time: Time,
) -> Result<CanisterManagerResponse, CanisterManagerError> {
    let sender = origin.origin();
    if sender != GOVERNANCE_CANISTER_ID.get() {
        validate_controller_or_subnet_admin(canister, subnet_admins, &sender)?;
    }

+   if canister.execution_state.is_none() {
+       return Err(CanisterManagerError::CanisterHasNoWasmModule(canister.canister_id()));
+   }

    let rejects = uninstall_canister(...);
    canister.add_canister_change(time, origin, CanisterChangeDetails::CanisterCodeUninstall);
    ...
}
```

This mirrors the pattern used in `MODULE_TYPE_FALLBACK` in the referenced EIP-7579 report, where a precondition check gates the destructive path. [7](#0-6) 

---

### Proof of Concept

1. Create a canister (no code installed; `execution_state` is `None`, `module_hash` is `None`).
2. Send an ingress message to `ic:00` with method `UninstallCode` targeting that canister.
3. Observe the call succeeds (returns `Ok`).
4. Call `canister_info` on the canister from another canister.
5. Observe `total_num_changes` has incremented and `recent_changes` contains a `code_uninstall` entry, even though no code was ever installed or removed.
6. Repeat step 2 N times; `total_num_changes` grows by N, each with a false `code_uninstall` record in the certified history.

This is directly confirmed by the existing test at `rs/execution_environment/src/canister_manager/tests.rs:4867` (`uninstall_code_on_empty_canister`), which asserts the call succeeds on a canister with `module_hash == None`. [3](#0-2) [8](#0-7)

### Citations

**File:** rs/execution_environment/src/canister_manager.rs (L933-976)
```rust
    pub(crate) fn uninstall_code(
        &self,
        origin: CanisterChangeOrigin,
        canister: &mut CanisterState,
        round_limits: &mut RoundLimits,
        subnet_admins: Option<BTreeSet<PrincipalId>>,
        time: Time,
    ) -> Result<CanisterManagerResponse, CanisterManagerError> {
        let sender = origin.origin();

        // Skip the controller or subnet admins validation if the sender is the
        // governance canister. The governance canister can forcefully
        // uninstall the code of any canister.
        if sender != GOVERNANCE_CANISTER_ID.get() {
            validate_controller_or_subnet_admin(canister, subnet_admins, &sender)?;
        }

        let rejects = uninstall_canister(
            &self.log,
            canister,
            Some(round_limits),
            time,
            Arc::clone(&self.fd_factory),
        );

        let available_execution_memory_change = canister.add_canister_change(
            time,
            origin,
            CanisterChangeDetails::CanisterCodeUninstall,
        );
        round_limits
            .subnet_available_memory
            .update_execution_memory_unchecked(available_execution_memory_change);

        Ok(CanisterManagerResponse {
            canister_id: canister.canister_id(),
            reply: Some(EmptyBlob.encode()),
            heap_delta_increase: NumBytes::new(0),
            unflushed_checkpoint_op: None,
            deleted_call_context_responses: rejects,
            stop_call_id_to_remove: None,
            stop_contexts_to_reject: vec![],
        })
    }
```

**File:** rs/execution_environment/src/canister_manager.rs (L3059-3074)
```rust
    // Drop the canister's execution state.
    canister.execution_state = None;

    // Clear canister log.
    canister.clear_log();

    // Clear the Wasm chunk store.
    canister.system_state.wasm_chunk_store = WasmChunkStore::new(fd_factory);

    // Drop its certified data.
    canister.system_state.certified_data = Vec::new();

    // Deactivate global timer.
    canister.system_state.global_timer = CanisterTimer::Inactive;
    // Increment canister version.
    canister.system_state.bump_canister_version();
```

**File:** rs/execution_environment/src/canister_manager/tests.rs (L4867-4890)
```rust
#[test]
fn uninstall_code_on_empty_canister() {
    const CYCLES: Cycles = Cycles::new(1_000_000_000_000_000);

    let mut test = ExecutionTestBuilder::new().build();
    let canister_id = test.create_canister(CYCLES);

    let empty_canister_status = test.canister_status(canister_id).unwrap();
    assert_eq!(empty_canister_status.status(), CanisterStatusType::Running);
    assert!(empty_canister_status.module_hash().is_none());

    test.uninstall_code(canister_id).unwrap();

    let uninstalled_canister_status = test.canister_status(canister_id).unwrap();
    assert_eq!(
        uninstalled_canister_status.status(),
        CanisterStatusType::Running
    );
    assert_eq!(
        uninstalled_canister_status.controllers(),
        empty_canister_status.controllers()
    );
    assert!(uninstalled_canister_status.module_hash().is_none());
}
```

**File:** rs/execution_environment/src/canister_manager/tests.rs (L4892-4939)
```rust
#[test]
fn uninstall_code_on_empty_canister_updates_subnet_available_memory() {
    const CYCLES: Cycles = Cycles::new(1_000_000_000_000_000);

    let mut test = ExecutionTestBuilder::new().build();
    let canister_id = test.create_canister(CYCLES);

    let canister_history_memory_usage = |test: &mut ExecutionTest| {
        let canister_state = test.canister_state(canister_id);
        let log_memory_store_memory_usage = canister_state.log_memory_store_memory_usage().get();
        let canister_history_memory_usage = canister_state.canister_history_memory_usage().get();
        let canister_memory_usage = canister_state.memory_usage().get();
        let canister_memory_allocated_bytes = canister_state.memory_allocated_bytes().get();
        assert_eq!(
            canister_history_memory_usage + log_memory_store_memory_usage,
            canister_memory_usage
        );
        assert_eq!(canister_memory_usage, canister_memory_allocated_bytes);
        canister_history_memory_usage
    };

    let initial_subnet_available_memory =
        test.subnet_available_memory().get_execution_memory() as u64;
    // Assert that canister history memory was non empty.
    let initial_canister_history_memory_usage = canister_history_memory_usage(&mut test);
    assert_gt!(initial_canister_history_memory_usage, 0);

    test.uninstall_code(canister_id).unwrap();

    let final_subnet_available_memory =
        test.subnet_available_memory().get_execution_memory() as u64;
    // Assert that canister history memory usage has increased.
    let final_canister_history_memory_usage = canister_history_memory_usage(&mut test);
    assert_gt!(
        final_canister_history_memory_usage,
        initial_canister_history_memory_usage
    );

    let extra_subnet_available_memory_usage =
        final_subnet_available_memory as i64 - initial_subnet_available_memory as i64;
    let extra_canister_history_memory_usage =
        final_canister_history_memory_usage as i64 - initial_canister_history_memory_usage as i64;
    // Assert that subnet available memory change has opposite sign to canister history memory change.
    assert_eq!(
        -extra_subnet_available_memory_usage,
        extra_canister_history_memory_usage
    );
}
```

**File:** rs/execution_environment/src/execution_environment.rs (L972-991)
```rust
            Ok(Ic00Method::UninstallCode) => match UninstallCodeArgs::decode(payload) {
                Err(err) => ExecuteSubnetMessageResult::Finished {
                    response: Err(err),
                    refund: msg.take_cycles(),
                },
                Ok(args) => {
                    let subnet_admins = state.get_own_subnet_admins();
                    let time = state.time();
                    self.uninstall_code(
                        msg.canister_change_origin(args.get_sender_canister_version()),
                        args.get_canister_id(),
                        &mut state,
                        &mut msg,
                        round_limits,
                        subnet_admins,
                        time,
                        current_round,
                    )
                }
            },
```

**File:** rs/execution_environment/src/execution_environment.rs (L2707-2731)
```rust
    fn get_canister_info(
        &self,
        canister_id: CanisterId,
        num_requested_changes: Option<u64>,
        state: &ReplicatedState,
    ) -> Result<Vec<u8>, UserError> {
        let canister = get_canister(canister_id, state)?;
        let canister_history = canister.system_state.get_canister_history();
        let total_num_changes = canister_history.get_total_num_changes();
        let changes = canister_history
            .get_changes(num_requested_changes.unwrap_or(0) as usize)
            .map(|e| (*e.clone()).clone())
            .collect();
        let module_hash = canister
            .execution_state
            .as_ref()
            .map(|es| es.wasm_binary.binary.module_hash().to_vec());
        let controllers = canister
            .controllers()
            .iter()
            .copied()
            .collect::<Vec<PrincipalId>>();
        let res = CanisterInfoResponse::new(total_num_changes, changes, module_hash, controllers);
        Ok(res.encode())
    }
```

**File:** rs/replicated_state/src/canister_state/system_state.rs (L415-427)
```rust
    pub fn add_canister_change(&mut self, canister_change: CanisterChange) {
        let changes = Arc::make_mut(&mut self.changes);
        if changes.len() >= MAX_CANISTER_HISTORY_CHANGES as usize {
            let change_size = changes
                .pop_front()
                .as_ref()
                .map(|c| c.count_bytes())
                .unwrap_or_default();
            self.canister_history_memory_usage -= change_size;
        }
        self.canister_history_memory_usage += canister_change.count_bytes();
        changes.push_back(Arc::new(canister_change));
        self.total_num_changes += 1;
```
