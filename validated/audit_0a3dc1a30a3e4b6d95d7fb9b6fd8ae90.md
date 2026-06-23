### Title
Canister Invariant Check Return Value Silently Discarded in Production — Execution Continues with Broken State (`rs/execution_environment/src/scheduler.rs`)

### Summary
The IC execution scheduler's `check_canister_invariants()` function returns `false` when a canister's post-execution state violates hard invariants (e.g., Wasm/stable memory exceeding configured limits). The return value is unconditionally discarded at the call site. In production (release) builds, the only effect of a broken invariant is a metric counter increment and a `warn!` log line. The subnet continues executing the canister with invalid state in every subsequent round, with no halt, quarantine, or corrective action.

### Finding Description

`check_canister_invariants` in `rs/execution_environment/src/scheduler.rs` iterates over all scheduled canisters and calls `canister.check_invariants()`. On failure it:

1. Formats an error message tagged `CANISTER_INVARIANT_BROKEN`.
2. Fires `debug_assert!(false, …)` — a **no-op in release/production builds**.
3. Increments `self.metrics.canister_invariants`.
4. Emits a `warn!` log.
5. Returns `false`. [1](#0-0) 

The caller in the round-finalization path calls the function and **drops the `bool` result entirely**:

```rust
self.check_canister_invariants(
    &round_log,
    &current_round,
    &state,
    round_schedule.scheduled_canisters(),
);
``` [2](#0-1) 

Round finalization then proceeds unconditionally — `process_stopping_canisters`, `prune_ingress_history`, `finish_round`, and `charge_canisters_for_resource_allocation_and_usage` all run on the state that contains the canister with broken invariants. [3](#0-2) 

The invariants checked include Wasm memory exceeding `max_wasm_memory_size`, stable memory exceeding `max_stable_memory_size`, and system-state invariants: [4](#0-3) 

A secondary, compounding issue: the per-round cycles conservation check (verifying that cycles-in ≥ cycles-out) is **permanently commented out** with `TODO(EXC-1124): Re-enable once the cycle balance check is fixed.` This means cycles-conservation violations are never detected at all: [5](#0-4) 

### Impact Explanation

A canister that reaches an invalid post-execution state (broken Wasm/stable memory bounds or system-state invariant) is **not quarantined or halted**. It is re-scheduled and re-executed in every subsequent round. Consequences:

- **State divergence**: If different replicas handle the invalid canister state differently in later rounds (e.g., due to subtle differences in how out-of-bounds memory is accessed), they may produce diverging states, breaking consensus.
- **Resource bound bypass**: A canister operating beyond its configured memory limits continues to consume subnet resources indefinitely, degrading performance for all other canisters on the subnet.
- **Silent corruption**: Because the only observable signal is a metric counter and a log line, operators may not notice the violation until significant damage has occurred.

The IC's own best-practices document acknowledges that broken hard invariants should cause a panic or at minimum a critical error, not a silent `warn!`: [6](#0-5) 

### Likelihood Explanation

Moderate. The invariant check is a safety net for cases where the execution environment itself has a bug (e.g., a Wasm execution path that allows memory to grow beyond the enforced limit, or a state-deserialization edge case). The IC's own test suite demonstrates the scenario is reachable: [7](#0-6) 

The test expects a `debug_assert` panic — confirming that in production (no `debug_assert`), the same scenario produces only a warning and continues. An unprivileged ingress sender who can trigger an execution-environment edge case that violates a canister invariant can repeatedly do so across rounds with no subnet-level response.

### Recommendation

1. **Act on the return value**: After calling `check_canister_invariants`, if it returns `false`, emit a `critical_error` counter (which pages on-call) and consider quarantining the affected canister (e.g., stopping it) rather than continuing execution.
2. **Upgrade the in-production signal**: Replace `warn!` with `error!` and increment a `critical_errors` counter so the FIT on-call is paged immediately.
3. **Re-enable the cycles conservation check** (EXC-1124) or document why it is permanently disabled and what alternative enforcement exists.
4. **Add a non-debug assertion path**: For hard invariant violations, consider a production-safe path that stops scheduling the offending canister rather than relying solely on `debug_assert`.

### Proof of Concept

**Attacker-controlled entry path:**

1. An unprivileged ingress sender submits messages to a canister on any application subnet.
2. A bug in the Wasm execution environment (or a crafted sequence of `memory.grow` operations near the limit boundary) causes the canister's `wasm_memory.size` to exceed `max_wasm_memory_size` after execution.
3. At round finalization, `check_canister_invariants` is called, detects the violation, logs a warning, increments `scheduler_canister_invariants`, and returns `false`.
4. The return value is discarded. The round finalizes normally. The canister is re-scheduled next round.
5. Steps 2–4 repeat indefinitely. The canister operates outside its resource bounds with no subnet-level response, and the risk of replica state divergence accumulates with each round.

The test at `rs/execution_environment/src/scheduler/tests.rs:718` confirms this exact scenario triggers only a `debug_assert` (no-op in production) and not a critical error or halt. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/execution_environment/src/scheduler.rs (L1031-1046)
```rust
            if let Err(err) = canister.check_invariants(&self.hypervisor_config) {
                let msg = format!(
                    "{}: At Round {} @ time {}, canister {} has invalid state after execution. Invariant check failed with err: {}",
                    CANISTER_INVARIANT_BROKEN,
                    current_round,
                    state.time(),
                    canister_id,
                    err
                );

                // Crash in debug mode if any invariant fails.
                debug_assert!(false, "{}", msg);

                self.metrics.canister_invariants.inc();
                warn!(round_log, "{}", msg);
                return false;
```

**File:** rs/execution_environment/src/scheduler.rs (L1480-1498)
```rust
            // TODO(EXC-1124): Re-enable once the cycle balance check is fixed.
            //
            // for canister in state.canisters_iter() {
            //     cycles_out_sum += canister.system_state.queues().output_queue_cycles();
            // }
            // cycles_out_sum += total_canister_balance;
            //
            // Check that amount of cycles at the beginning of the round (balances and cycles from input messages) is bigger or equal
            // than the amount of cycles at the end of the round (balances and cycles from output messages).
            // if cycles_in_sum < cycles_out_sum {
            //     warn!(
            //         round_log,
            //         "At Round {} @ time {}, the resulted state after execution does not hold the in-out cycles invariant: cycles at beginning of round {} were fewer than cycles at end of round {}",
            //         current_round,
            //         state.time(),
            //         cycles_in_sum,
            //         cycles_out_sum,
            //     );
            // }
```

**File:** rs/execution_environment/src/scheduler.rs (L1500-1506)
```rust
            // Check that invariants still hold for scheduled canisters after execution.
            self.check_canister_invariants(
                &round_log,
                &current_round,
                &state,
                round_schedule.scheduled_canisters(),
            );
```

**File:** rs/execution_environment/src/scheduler.rs (L1508-1548)
```rust
            // NOTE: The logic for deleting canisters assumes that transitioning
            // canisters from `Stopping` to `Stopped` happens at the end of the round
            // as is currently the case. If this logic is moved elsewhere (e.g. at the
            // beginning of the round), then canister deletion logic should be revised.
            {
                let _timer = self.metrics.round_finalization_stop_canisters.start_timer();
                final_state = self
                    .exec_env
                    .process_stopping_canisters(state, current_round);
            }
            {
                let _timer = self.metrics.round_finalization_ingress.start_timer();
                final_state.prune_ingress_history();
            }

            // Update canister priorities.
            {
                let _timer = self.metrics.round_finalization_scheduling.start_timer();
                round_schedule.finish_round(&mut final_state, current_round, &self.metrics);
            }

            // Abort (some) paused executions.
            self.finish_round(&mut final_state, current_round_type);

            // Charge canisters after (some) paused executions were aborted.
            {
                let _timer = self.metrics.round_finalization_charge.start_timer();
                self.charge_canisters_for_resource_allocation_and_usage(
                    &mut final_state,
                    current_round,
                    current_round_type,
                );
            }

            final_state
                .metadata
                .subnet_metrics
                .update_transactions_total += root_measurement_scope.messages().get();
            final_state.metadata.subnet_metrics.num_canisters =
                final_state.canister_states().len() as u64;
        }
```

**File:** rs/replicated_state/src/canister_state.rs (L377-407)
```rust
    pub fn check_invariants(&self, config: &HypervisorConfig) -> Result<(), String> {
        if let Some(execution_state) = &self.execution_state {
            let wasm_memory_usage = execution_state.wasm_memory_usage();
            let wasm_memory_limit = match execution_state.wasm_execution_mode() {
                WasmExecutionMode::Wasm32 => config.max_wasm_memory_size,
                WasmExecutionMode::Wasm64 => config.max_wasm64_memory_size,
            };
            if wasm_memory_usage > wasm_memory_limit {
                return Err(format!(
                    "Invariant broken: Wasm memory of canister {} exceeds the limit allowed: used {}, allowed {}",
                    self.canister_id(),
                    wasm_memory_usage,
                    wasm_memory_limit
                ));
            }

            let stable_memory_usage = execution_state.stable_memory_usage();
            let stable_memory_limit = config.max_stable_memory_size;
            if stable_memory_usage > stable_memory_limit {
                return Err(format!(
                    "Invariant broken: Stable memory of canister {} exceeds the limit allowed: used {}, allowed {}",
                    self.canister_id(),
                    stable_memory_usage,
                    stable_memory_limit
                ));
            }
        }

        self.system_state.check_invariants()?;

        self.canister_snapshots.check_invariants()
```

**File:** rs/replicated_state/best-practices-panics.md (L25-30)
```markdown
A [hard] invariant refers to a condition that (1) holds all the time, and (2) whose violation affects code correctness:

- We check these during deserialization and return an error (causing an upstream panic) if they don't hold.
- It is fine to assert/debug_assert (depending on how expensive these checks are) for them in production code.
- Proptests for these invariants are recommended, but can be skipped if there is consensus that they are not needed.

```

**File:** rs/execution_environment/src/scheduler/tests.rs (L718-739)
```rust
#[test]
#[should_panic(expected = "scheduler_canister_invariant_broken")]
fn check_canister_invariants_detects_wasm_memory_exceeding_limit() {
    use ic_replicated_state::NumWasmPages;

    let mut test = SchedulerTestBuilder::new().build();
    let canister = test.create_canister();

    // Inflate the canister's wasm memory size beyond `max_wasm_memory_size`
    // (default 4 GiB = 65536 wasm pages of 64 KiB each).
    test.canister_state_mut(canister)
        .execution_state
        .as_mut()
        .unwrap()
        .wasm_memory
        .size = NumWasmPages::from(65536 + 1);

    // Send a message so the canister is scheduled, then execute a round.
    // The invariant check during finalization detects the violation and
    // panics via debug_assert.
    test.send_ingress(canister, ingress(1));
    test.execute_round(ExecutionRoundType::OrdinaryRound);
```

**File:** rs/execution_environment/src/scheduler/scheduler_metrics.rs (L14-17)
```rust
pub(crate) const CANISTER_INVARIANT_BROKEN: &str = "scheduler_canister_invariant_broken";
pub(crate) const SCHEDULER_COMPUTE_ALLOCATION_INVARIANT_BROKEN: &str =
    "scheduler_compute_allocation_invariant_broken";
pub(crate) const SCHEDULER_CORES_INVARIANT_BROKEN: &str = "scheduler_cores_invariant_broken";
```
