Audit Report

## Title
`validate_cycle_change` Missing Balance Sufficiency Check Allows Compromised Sandbox to Panic Replica via `reserve_cycles().unwrap()` — (`rs/embedders/src/wasmtime_embedder/system_api/sandbox_safe_system_state.rs`)

## Summary

`validate_cycle_change` enforces that no cycles are created from thin air by checking `cycles_balance_change == expected_change`, but it does not verify that `reserved_cycles ≤ canister_balance`. A compromised sandbox can craft a `SystemStateModifications` payload with `reserved_cycles = R > B` and `cycles_balance_change = Removed(R)` that passes this guard, then causes `state.reserve_cycles(self.reserved_cycles).unwrap()` at line 600 of `apply_balance_changes` to panic, crashing the replica process.

## Finding Description

**Guard: `validate_cycle_change`**

The function at lines 160–191 builds `expected_change` by accumulating `call_context_balance_taken`, outgoing request payments, consumed cycles, and `reserved_cycles`, then asserts `cycles_balance_change == expected_change`. It does not check `reserved_cycles ≤ state.balance()`. [1](#0-0) 

**Crafted payload that passes the guard:**

Set `reserved_cycles = R` (where `R > canister_balance B`) and `cycles_balance_change = Removed(R)`, all other fields zero/empty.
- `expected_change = Removed(R)` (only the `reserved_cycles` term contributes at line 176)
- `cycles_balance_change = Removed(R)`
- Equality holds → `validate_cycle_change` returns `Ok(())`

**What happens in `apply_balance_changes`:**

`adjusted_balance_change` starts as `Removed(R)`, then `Added(R)` is added to exclude reserved cycles (line 572–573). Since `R == R`, the result is `Added(0)`, so the balance stays at `B`. Then `state.reserve_cycles(R).unwrap()` is called at line 600. [2](#0-1) 

**`reserve_cycles` returns `Err` when `R > B`:**

`can_reserve_cycles` at line 2085 returns `Err(ReservationError::InsufficientCycles)` when `amount > main_balance`. The `.unwrap()` at line 600 panics. The comment at lines 597–599 explicitly acknowledges this is intentional ("it is better to crash here"), but the assumption that `validate_cycle_change` would have caught this case is incorrect. [3](#0-2) 

There is also a secondary panic point: the `assert_eq!(state.balance(), expected_balance)` at line 608 would fire because `state.balance() = B` but `expected_balance = B - R`. [4](#0-3) 

**IPC path from sandbox to replica:**

`SystemStateModifications` is embedded in `StateModifications` → `SandboxExecOutput` → `ExecutionFinishedRequest`, which is deserialized directly from the sandbox IPC socket. [5](#0-4) 

The `execution_finished` handler in `ControllerServiceImpl` passes `exec_output` directly to the completion closure without any additional validation of the `system_state_modifications` fields beyond what `apply_changes` performs. [6](#0-5) 

The completion closure calls `apply_canister_state_changes` → `try_apply_canister_state_changes` → `apply_changes` → `apply_balance_changes`, all in the replica's execution thread. [7](#0-6) 

`apply_canister_state_changes` handles `Err` returns from `try_apply_canister_state_changes` but does not catch panics; a panic propagates up and crashes the replica thread. [8](#0-7) 

## Impact Explanation

A panic in `apply_balance_changes` is not caught by the `HypervisorResult` error handling in `apply_canister_state_changes`. The panic propagates up the call stack and crashes the replica process. This constitutes an application/platform-level DoS and subnet availability impact. This matches the **High ($2,000–$10,000)** impact class: "Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS."

## Likelihood Explanation

Requires a prior Wasm sandbox escape to gain arbitrary IPC write capability. However, the IC's security model explicitly treats the sandbox as a trust boundary — `validate_cycle_change` exists precisely to validate sandbox-provided data against this boundary. The gap is concrete, locally testable, and the exploit payload is trivial to construct once the IPC channel is controlled. A compromised sandbox can repeatedly trigger this to prevent the replica from recovering.

## Recommendation

1. **Primary fix:** Add a balance sufficiency check inside `validate_cycle_change`, passing in the current canister balance (already available at the call site in `apply_changes` via `system_state`):
   ```rust
   if self.reserved_cycles > canister_balance {
       return Err(Self::error("reserved_cycles exceeds canister balance"));
   }
   ```

2. **Defense-in-depth:** Replace `.unwrap()` at line 600 with a graceful error return so that even if the validation gap is missed in the future, the replica does not crash:
   ```rust
   state.reserve_cycles(self.reserved_cycles)
       .map_err(|e| Self::error(format!("Failed to reserve cycles: {:?}", e)))?;
   ```

## Proof of Concept

```rust
#[test]
#[should_panic]
fn test_reserve_cycles_panic_with_insufficient_balance() {
    let canister_balance = Cycles::new(1_000);
    let reserved = Cycles::new(2_000); // > balance

    let mut system_state = SystemState::new_running_for_testing(
        canister_test_id(0),
        user_test_id(1).get(),
        canister_balance,
        NumSeconds::from(0),
    );

    // Craft modifications that pass validate_cycle_change:
    // cycles_balance_change = Removed(reserved) matches expected_change = Removed(reserved)
    let mods = SystemStateModifications {
        cycles_balance_change: CyclesBalanceChange::Removed(reserved),
        reserved_cycles: reserved,
        ..Default::default()
    };

    // validate_cycle_change passes (no cycles created)
    assert!(mods.validate_cycle_change(false).is_ok());

    // apply_balance_changes panics at reserve_cycles(2000).unwrap()
    // because balance is only 1000
    mods.apply_balance_changes(&mut system_state);
}
```

### Citations

**File:** rs/embedders/src/wasmtime_embedder/system_api/sandbox_safe_system_state.rs (L160-191)
```rust
    fn validate_cycle_change(&self, is_cmc_canister: bool) -> HypervisorResult<()> {
        let mut expected_change = CyclesBalanceChange::zero();

        if let Some((_, call_context_balance_taken)) = self.call_context_balance_taken {
            expected_change =
                expected_change + CyclesBalanceChange::added(call_context_balance_taken);
        }

        for req in self.requests.iter() {
            expected_change = expected_change + CyclesBalanceChange::removed(req.payment);
        }

        for amount in self.consumed_cycles_by_use_case.iter_over_real() {
            expected_change = expected_change + CyclesBalanceChange::removed(amount);
        }

        expected_change = expected_change + CyclesBalanceChange::removed(self.reserved_cycles);

        // If the canister is not the cycles minting canister, then the balance
        // change coming from the Wasm execution must match the expected balance
        // change that we just computed.
        if is_cmc_canister || self.cycles_balance_change == expected_change {
            Ok(())
        } else {
            Err(HypervisorError::WasmEngineError(
                WasmEngineError::FailedToApplySystemChanges(format!(
                    "Invalid cycle change: expected {:?}, got {:?}",
                    expected_change, self.cycles_balance_change
                )),
            ))
        }
    }
```

**File:** rs/embedders/src/wasmtime_embedder/system_api/sandbox_safe_system_state.rs (L566-600)
```rust
        let mut adjusted_balance_change = self.cycles_balance_change;
        for amount in self.consumed_cycles_by_use_case.iter_over_real() {
            adjusted_balance_change = adjusted_balance_change + CyclesBalanceChange::added(amount)
        }

        // Exclude the reserved cycles.
        adjusted_balance_change =
            adjusted_balance_change + CyclesBalanceChange::added(self.reserved_cycles);

        // Apply the main cycles balance change without the consumed and reserved cycles.
        match adjusted_balance_change {
            CyclesBalanceChange::Added(added) => state.add_cycles(added),
            CyclesBalanceChange::Removed(removed) => state.remove_cycles(removed),
        }

        // Apply the consumed cycles with the use case metrics recording.
        let ConsumedCyclesDuringExecution {
            burned,
            instructions,
            request_and_response_transmission,
        } = self.consumed_cycles_by_use_case;
        if let Some(x) = burned {
            state.consume_cycles(x);
        }
        if let Some(x) = instructions {
            state.consume_cycles(x);
        }
        if let Some(x) = request_and_response_transmission {
            state.consume_cycles(x);
        }

        // Apply the reserved cycles. This must succeed because the cycle
        // changes were validated. If it doesn't succeed then, it is better to
        // crash here to avoid making the cycle balance incorrect.
        state.reserve_cycles(self.reserved_cycles).unwrap();
```

**File:** rs/embedders/src/wasmtime_embedder/system_api/sandbox_safe_system_state.rs (L604-608)
```rust
        let expected_balance = match self.cycles_balance_change {
            CyclesBalanceChange::Added(added) => initial_balance + added,
            CyclesBalanceChange::Removed(removed) => initial_balance - removed,
        };
        assert_eq!(state.balance(), expected_balance);
```

**File:** rs/replicated_state/src/canister_state/system_state.rs (L2085-2101)
```rust
        if amount > main_balance {
            Err(ReservationError::InsufficientCycles {
                requested: amount,
                available: main_balance,
            })
        } else {
            Ok(())
        }
    }

    /// Moves the given amount of cycles from the main balance to the reserved balance.
    /// Returns an error if the main balance is lower than the requested amount.
    pub fn reserve_cycles(&mut self, amount: Cycles) -> Result<(), ReservationError> {
        self.can_reserve_cycles(amount, self.cycles_balance)?;
        self.cycles_balance -= amount;
        self.reserved_balance += amount;
        Ok(())
```

**File:** rs/canister_sandbox/src/protocol/structs.rs (L60-74)
```rust
#[derive(Clone, PartialEq, Debug, Default, Deserialize, Serialize)]
pub struct StateModifications {
    /// Modifications in the execution state of the canister.
    ///
    /// This field is optional because the state changes might or might not
    /// be applied depending on the method executed.
    pub execution_state_modifications: Option<ExecutionStateModifications>,

    /// Modifications in the system state of the canister.
    ///
    /// The system state changes contain parts that are always applied
    /// and parts that are only applied depending on the method executed
    /// (similarly to `execution_state_modifications`).
    pub system_state_modifications: SystemStateModifications,
}
```

**File:** rs/canister_sandbox/src/replica_controller/controller_service_impl.rs (L63-79)
```rust
        let reply = self.registry.take(exec_id).map_or_else(
            || {
                // Should we log the entire erroneous request? It
                // could both be large and hold canister-sensitive
                // data, so maybe this is not advisable.
                error!(
                    self.log,
                    "Wasm sandbox process sent completion for non-existent execution {}", &exec_id
                );
                Err(rpc::Error::ServerError)
            },
            |completion| {
                completion(exec_id, CompletionResult::Finished(exec_output));
                Ok(protocol::ctlsvc::ExecutionFinishedReply {})
            },
        );
        rpc::Call::new_resolved(reply)
```

**File:** rs/execution_environment/src/execution/common.rs (L501-530)
```rust
fn try_apply_canister_state_changes(
    system_state_modifications: SystemStateModifications,
    output: &WasmExecutionOutput,
    system_state: &mut SystemState,
    subnet_available_memory: &mut SubnetAvailableMemory,
    time: Time,
    network_topology: &NetworkTopology,
    subnet_id: SubnetId,
    is_composite_query: bool,
    metrics: &HypervisorMetrics,
    log: &ReplicaLogger,
) -> HypervisorResult<RequestMetadataStats> {
    subnet_available_memory
        .try_decrement(
            output.allocated_bytes,
            output.allocated_guaranteed_response_message_bytes,
            NumBytes::from(0),
        )
        .map_err(|_| HypervisorError::OutOfMemory)?;

    system_state_modifications.apply_changes(
        time,
        system_state,
        network_topology,
        subnet_id,
        is_composite_query,
        metrics,
        log,
    )
}
```

**File:** rs/execution_environment/src/execution/common.rs (L569-623)
```rust
    match try_apply_canister_state_changes(
        system_state_modifications,
        output,
        system_state,
        &mut round_limits.subnet_available_memory,
        time,
        network_topology,
        subnet_id,
        is_composite_query,
        metrics,
        log,
    ) {
        Ok(request_stats) => {
            if let Some(ExecutionStateChanges {
                globals,
                wasm_memory,
                stable_memory,
            }) = execution_state_changes
            {
                execution_state.wasm_memory = wasm_memory;
                execution_state.stable_memory = stable_memory;
                execution_state.exported_globals = globals;
            }
            round_limits.subnet_available_callbacks -= callbacks_created as i64;
            deallocate(clean_system_state);

            call_tree_metrics.observe(request_stats, call_context_creation_time, time);
        }
        Err(err) => {
            debug_assert_eq!(err, HypervisorError::OutOfMemory);
            match &err {
                HypervisorError::WasmEngineError(err) => {
                    state_changes_error.inc();
                    error!(
                        log,
                        "[EXC-BUG]: Failed to apply state changes due to a bug: {}", err
                    )
                }
                HypervisorError::OutOfMemory => {
                    warn!(log, "Failed to apply state changes due to DTS: {}", err)
                }
                _ => {
                    state_changes_error.inc();
                    error!(
                        log,
                        "[EXC-BUG]: Failed to apply state changes due to an unexpected error: {}",
                        err
                    )
                }
            }
            let old_system_state = std::mem::replace(system_state, clean_system_state);
            deallocate(old_system_state);
            round_limits.subnet_available_memory = clean_subnet_available_memory;
            output.wasm_result = Err(err);
        }
```
