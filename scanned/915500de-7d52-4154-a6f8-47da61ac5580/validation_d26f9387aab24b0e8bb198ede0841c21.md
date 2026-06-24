Based on my analysis of the actual code, this is a real vulnerability. Let me trace through the exact mechanism.

---

### Title
DTS Long-Execution Starvation via `executed_rounds` Accumulation in `abort_paused_executions_above_limit` — (`rs/execution_environment/src/scheduler.rs`)

### Summary

The `executed_rounds.max(1)` anti-starvation clamp in `CanisterRoundState::Ord` only prevents starvation in the **first round** after an abort. Once K attacker-controlled canisters accumulate `executed_rounds >= 2`, the victim canister (perpetually reset to `executed_rounds = 0`, treated as 1) is always ranked below all K attacker canisters and is aborted every round indefinitely.

### Finding Description

`abort_paused_executions_above_limit` sorts all paused-execution canisters by `CanisterRoundState::Ord` and aborts those beyond index `max_paused_executions`: [1](#0-0) 

The sort order for long executions is: [2](#0-1) 

The comment explicitly acknowledges the starvation risk and claims the `.max(1)` clamp fixes it: [3](#0-2) 

When a canister is aborted, only `executed_rounds` is reset to 0; `accumulated_priority` and `long_execution_start_round` are preserved: [4](#0-3) 

`finish_round` increments `executed_rounds` for every canister that executes a slice, **before** `abort_paused_executions_above_limit` runs: [5](#0-4) 

The call order in `execute_round` is: `round_schedule.finish_round()` (increments `executed_rounds`) → `self.finish_round()` (calls `abort_paused_executions_above_limit`): [6](#0-5) 

**Round-by-round trace** with `max_paused_executions = K = 4` (production default): [7](#0-6) 

- **Round 1**: All K+1 canisters execute a slice. `finish_round` increments all to `executed_rounds = 1`. `abort_paused_executions_above_limit` runs: all have `executed_rounds.max(1) = 1`, so AP/start-round tiebreakers decide. Victim (lower AP or later start) is aborted → `executed_rounds` reset to 0.
- **Round 2**: Attacker's K canisters execute another slice → `executed_rounds = 2`. Victim re-executes one slice (it was aborted, re-scheduled as `ContinueLong`) → `executed_rounds = 1`. Abort runs: attacker = 2 > victim = 1. Victim aborted again → reset to 0.
- **Round N**: Attacker has `executed_rounds = N`. Victim always has `executed_rounds = 0` (treated as 1). Gap grows unboundedly. Victim is perpetually aborted.

The `.max(1)` clamp only equalizes `executed_rounds = 0` with `executed_rounds = 1`. It provides no protection once attacker canisters reach `executed_rounds >= 2`, which happens after just one round of uncontested execution.

### Impact Explanation

The victim canister's long DTS execution never completes. Its ingress messages remain in `Processing` state indefinitely. This is a complete liveness denial for the victim canister — all queued messages are effectively blocked forever. The attacker does not need to crash or corrupt any node; the attack is purely through normal canister execution.

### Likelihood Explanation

- Requires only K = 4 canisters (production `max_paused_executions`), each sending a long message (many slices up to `max_instructions_per_message`).
- The attacker must maintain K concurrent long executions. This is achievable by continuously sending new long messages as old ones complete, or by sending a single message requiring many hundreds of slices.
- No privileged access, governance majority, or threshold corruption required — purely unprivileged canister developer actions via ingress.
- The attack is stable: once established (after round 1), it self-perpetuates with no further attacker intervention needed.
- Cycles cost to the attacker is bounded (they pay per slice executed), but the victim pays nothing for aborted slices, so the attacker bears the cost of their own K long executions only.

### Recommendation

Replace the `executed_rounds.max(1)` clamp with a mechanism that gives aborted executions a **monotonically increasing** priority boost proportional to how many times they have been aborted, or use a separate "abort count" field that is never reset. Alternatively, treat aborted executions as having the highest priority among long executions (i.e., sort them first, not equal to `executed_rounds = 1`). A simpler fix: when a canister is aborted by `abort_paused_executions_above_limit`, set its `executed_rounds` to the maximum `executed_rounds` among all currently paused canisters plus 1, ensuring it wins the next round's priority comparison.

### Proof of Concept

```rust
// Pseudocode proptest: K=4 attacker canisters, 1 victim
// max_paused_executions = 4
// Each attacker canister: long message requiring 1000 slices, high CA
// Victim: long message requiring 10 slices, CA=0

// Setup: attacker starts K long executions first (round 0)
// Victim starts long execution (round 1)

// After round 1: attacker executed_rounds=1, victim executed_rounds=1
//   → victim aborted (lower AP), executed_rounds reset to 0

// After round 2: attacker executed_rounds=2, victim executed_rounds=1
//   → victim aborted (2 > 1), executed_rounds reset to 0

// After round N: attacker executed_rounds=N, victim executed_rounds=1
//   → victim always aborted

// Assert: victim message never completes within 10000 rounds
// (it would need only 10 uninterrupted rounds to complete)
for round in 0..10000 {
    test.execute_round(OrdinaryRound);
    // victim's executed_rounds oscillates between 0 and 1 every round
    assert_eq!(victim_executed_rounds, 0); // always reset
}
assert!(victim_message_still_processing); // liveness violation
```

The existing `respect_max_paused_executions` proptest at `rs/execution_environment/src/scheduler/tests/dts.rs:268` does not cover this adversarial case because it creates canisters with equal compute allocations and no pre-accumulated `executed_rounds` advantage. [8](#0-7)

### Citations

**File:** rs/execution_environment/src/scheduler.rs (L1068-1082)
```rust
        paused_round_states.sort();

        paused_round_states
            .iter()
            .skip(self.config.max_paused_executions)
            .for_each(|rs| {
                let canister = canister_states.get_mut(&rs.canister_id()).unwrap();
                abort_canister(
                    canister,
                    subnet_schedule,
                    &self.exec_env,
                    cost_schedule,
                    &self.log,
                );
            });
```

**File:** rs/execution_environment/src/scheduler.rs (L1524-1530)
```rust
            {
                let _timer = self.metrics.round_finalization_scheduling.start_timer();
                round_schedule.finish_round(&mut final_state, current_round, &self.metrics);
            }

            // Abort (some) paused executions.
            self.finish_round(&mut final_state, current_round_type);
```

**File:** rs/execution_environment/src/scheduler.rs (L2203-2208)
```rust
    if exec_env.abort_canister(canister, log, cost_schedule) {
        // Reset `executed_rounds` to zero.
        subnet_schedule
            .get_mut(canister.canister_id())
            .executed_rounds = 0;
    }
```

**File:** rs/execution_environment/src/scheduler/round_schedule.rs (L115-126)
```rust
            //
            // An aborted execution (executed rounds == 0) is considered to have the same
            // priority as a newly started long execution (executed rounds == 1). This is to
            // avoid starvation of aborted executions.
            (Some(self_start_round), Some(other_start_round)) => other
                .executed_rounds
                .max(1)
                .cmp(&self.executed_rounds.max(1))
                .then_with(|| other.accumulated_priority.cmp(&self.accumulated_priority))
                .then_with(|| self_start_round.cmp(&other_start_round))
                .then_with(|| self.canister_id.cmp(&other.canister_id)),
        }
```

**File:** rs/execution_environment/src/scheduler/round_schedule.rs (L534-537)
```rust
        for canister_id in self.fully_executed_canisters.iter() {
            let canister_priority = subnet_schedule.get_mut(*canister_id);
            canister_priority.executed_rounds += 1;
            canister_priority.last_full_execution_round = current_round;
```

**File:** rs/config/src/subnet_config.rs (L125-125)
```rust
const MAX_PAUSED_EXECUTIONS: usize = 4;
```

**File:** rs/execution_environment/src/scheduler/tests/dts.rs (L268-321)
```rust
#[test_strategy::proptest]
fn respect_max_paused_executions(
    #[strategy(2..10_usize)] scheduler_cores: usize,
    #[strategy(1..10_usize)] num_canisters: usize,
    #[strategy(1..10_u64)] num_slices: u64,
    #[strategy(1..2.max(#num_canisters - 1))] max_paused_executions: usize,
) {
    let mut test = SchedulerTestBuilder::new()
        .with_scheduler_config(SchedulerConfig {
            scheduler_cores,
            instruction_overhead_per_execution: NumInstructions::from(0),
            instruction_overhead_per_canister: NumInstructions::from(0),
            max_instructions_per_round: NumInstructions::from(100 * num_slices),
            max_instructions_per_message: NumInstructions::from(100 * num_slices),
            max_instructions_per_slice: NumInstructions::from(100),
            max_instructions_per_install_code_slice: NumInstructions::from(100),
            max_paused_executions,
            ..SchedulerConfig::application_subnet()
        })
        .build();

    let mut message_ids = vec![];
    for _ in 0..num_canisters {
        let canister_id = test.create_canister();
        let message_id = test.send_ingress(canister_id, ingress(100 * num_slices));
        message_ids.push(message_id);
    }

    test.execute_all_with(|test| {
        let (canister_states, subnet_schedule) = test.state_mut().canisters_and_schedule_mut();
        let paused_executions = canister_states
            .hot_values()
            .filter(|canister| {
                let priority = subnet_schedule.get(&canister.canister_id());
                if canister.has_paused_execution() {
                    // All paused executions have non-zero executed rounds.
                    assert_ne!(priority.executed_rounds, 0);
                    true
                } else {
                    // All aborted (or not started) executions have zero executed rounds.
                    assert_eq!(priority.executed_rounds, 0);
                    false
                }
            })
            .count();
        // Make sure the `max_paused_executions` is respected after each round
        assert_le!(paused_executions, max_paused_executions);
    });

    // Make sure all the messages are complete
    for message_id in message_ids.iter() {
        let message_error = test.ingress_error(message_id).code();
        assert_eq!(message_error, ErrorCode::CanisterDidNotReply,);
    }
```
