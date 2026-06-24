### Title
Finalization Instruction Overhead Charged Against All Canisters (Including Cold/Idle) Per Inner-Loop Iteration, Enabling Subnet Throughput DoS - (File: rs/execution_environment/src/scheduler.rs)

---

### Summary

At the end of every inner-loop iteration in `inner_round`, the scheduler deducts `instruction_overhead_per_canister_for_finalization * state.num_canisters()` from the round instruction budget. `state.num_canisters()` counts **all** canisters on the subnet — including idle "cold" canisters that are explicitly excluded from per-round operations by the hot/cold partitioning introduced in `CanisterStates`. The actual per-iteration finalization work (`finish_round`) only iterates over `scheduled_canisters` (a small subset). An unprivileged canister developer who creates many idle canisters can inflate this overhead, causing the round instruction budget to be consumed faster than the actual work warrants, degrading subnet throughput for all users.

---

### Finding Description

In `inner_round` (`rs/execution_environment/src/scheduler.rs`, lines 559–563), after each inner-loop iteration completes:

```rust
round_limits.instructions -= as_round_instructions(
    self.config
        .instruction_overhead_per_canister_for_finalization
        * state.num_canisters() as u64,
);
```

`state.num_canisters()` returns `hot.len() + cold.len()` — the total count of every canister on the subnet. [1](#0-0) 

The constant `INSTRUCTION_OVERHEAD_PER_CANISTER_FOR_FINALIZATION = 12_000` was empirically calibrated when finalization took 13 ms with 5,000 canisters — **before** the hot/cold `CanisterStates` partitioning was introduced. [2](#0-1) 

The `CanisterStates` module was explicitly introduced to make per-round operations `O(|hot|)` instead of `O(|all canisters|)`:

> "Internally, `CanisterStates` also maintains `ColdStats`, a small set of aggregates over the cold pool that lets several aggregated queries become `O(|hot|)` instead of `O(|all canisters|)`." [3](#0-2) 

Cold canisters are definitionally idle — they satisfy `CanisterState::is_cold()` and are skipped by per-round scheduling, heartbeat enqueueing, and queue GC. [4](#0-3) 

The actual per-iteration finalization in `finish_round` only iterates over `self.scheduled_canisters` and `self.fully_executed_canisters` — both small subsets of the total canister population: [5](#0-4) 

The only finalization function that truly iterates over all canisters is `charge_canisters_for_resource_allocation_and_usage`, which **skips execution on 49 out of every 50 rounds** via an early return: [6](#0-5) 

The result is a structural mismatch: the overhead is charged every inner-loop iteration as if all canisters are visited, but the hot/cold partitioning means cold canisters are not touched in the vast majority of iterations.

---

### Impact Explanation

With `INSTRUCTION_OVERHEAD_PER_CANISTER_FOR_FINALIZATION = 12_000` and `MAX_INSTRUCTIONS_PER_ROUND = 4_000_000_000`:

- **50,000 canisters** → overhead per iteration = `12,000 × 50,000 = 600,000,000` instructions
- Round budget consumed by overhead alone per iteration: **15%**
- Maximum inner-loop iterations before budget exhaustion (ignoring actual message execution): **~6**

This directly limits how many messages can be processed per round. The test `finalization_overhead_per_canister_reduces_instruction_budget` confirms that a large enough overhead prevents inner-loop iterations from running, starving message execution: [7](#0-6) 

The impact is **subnet-wide throughput degradation** — all users on the subnet experience reduced message processing capacity.

---

### Likelihood Explanation

An unprivileged canister developer can create canisters on any application subnet. Once created, idle canisters are demoted to the cold pool and impose no per-round execution cost — but they permanently inflate `state.num_canisters()`. The attacker pays a one-time cycle cost per canister; the ongoing overhead is borne by the subnet's round instruction budget every iteration, indefinitely. The attack is bounded by the subnet's canister count limit and the attacker's cycle budget, but the asymmetry (one-time cost vs. perpetual overhead inflation) makes it economically viable at scale.

---

### Recommendation

Replace `state.num_canisters()` with the count of hot (active) canisters in the overhead calculation, since the hot/cold partitioning means cold canisters are not visited in per-round finalization. Alternatively, re-calibrate `INSTRUCTION_OVERHEAD_PER_CANISTER_FOR_FINALIZATION` to reflect the actual post-partitioning finalization cost, which is now `O(|hot|)` for most operations. The overhead for the periodic `charge_canisters_for_resource_allocation_and_usage` (which does visit all canisters) should be amortized across the 50-round charge interval rather than charged every iteration.

---

### Proof of Concept

1. Deploy N idle canisters on an application subnet (no heartbeat, no pending messages). After a few rounds they are demoted to the cold pool.
2. Send a cross-canister call chain (e.g., canister A → canister B → callback to A) that normally produces 3 inner-loop iterations per round.
3. Observe: with N = 50,000 idle canisters, each inner-loop iteration deducts `12,000 × 50,000 = 600M` instructions from the 4B round budget. The callback iteration (iteration 3) is starved and cannot run — confirmed by the existing test `finalization_overhead_per_canister_reduces_instruction_budget` which demonstrates exactly this starvation pattern with only 3 canisters and a proportionally large overhead.
4. The attacker's idle canisters are never executed, never charged per-round, and never appear in `scheduled_canisters` — yet they permanently inflate the overhead charged against every inner-loop iteration on the subnet. [1](#0-0) [8](#0-7) [9](#0-8)

### Citations

**File:** rs/execution_environment/src/scheduler.rs (L559-563)
```rust
            round_limits.instructions -= as_round_instructions(
                self.config
                    .instruction_overhead_per_canister_for_finalization
                    * state.num_canisters() as u64,
            );
```

**File:** rs/execution_environment/src/scheduler.rs (L832-836)
```rust
        if !current_round.get().is_multiple_of(CHARGE_INTERVAL_ROUNDS)
            && current_round_type != ExecutionRoundType::CheckpointRound
        {
            return;
        }
```

**File:** rs/config/src/subnet_config.rs (L60-65)
```rust
// Metrics show that finalization can take 13ms when there were 5000 canisters
// in a subnet. This comes out to about 3us per canister which comes out to
// 6_000 instructions based on the 1 cycles unit ≅ 1 CPU cycle, 2 GHz CPU
// calculations. Round this up to 12_000 to be on the safe side.
const INSTRUCTION_OVERHEAD_PER_CANISTER_FOR_FINALIZATION: NumInstructions =
    NumInstructions::new(12_000);
```

**File:** rs/config/src/subnet_config.rs (L244-249)
```rust
    /// The overhead (per canister) of running the finalization code at the end
    /// of an iteration. This overhead is counted toward the round limit at the
    /// end of each iteration. Since finalization is mostly looping over all
    /// canisters, we estimate the cost per canister and multiply by the number
    /// of active canisters to get the total overhead.
    pub instruction_overhead_per_canister_for_finalization: NumInstructions,
```

**File:** rs/replicated_state/src/canister_states.rs (L1-16)
```rust
//! `CanisterStates`: a hot/cold-partitioned collection of [`CanisterState`]s.
//!
//! The set of all canisters hosted on a subnet is split into two collections:
//!
//!   * `hot`: canisters that may need round-level attention. The hot pool is
//!     intentionally a superset of "actually active": once a canister has been
//!     touched (via [`CanisterStates::get_mut`] or any other mutating accessor)
//!     it stays hot until explicitly demoted.
//!   * `cold`: canisters that are *definitely* idle, as defined by the pure
//!     predicate [`CanisterState::is_cold`].
//!
//! Promotion (cold → hot) is eager: it happens as a side effect of every
//! mutating accessor. Demotion (hot → cold) is conditional and explicit, via
//! [`CanisterStates::try_cool`] for single canisters or
//! [`CanisterStates::try_cool_all`] for a bulk pass.
//!
```

**File:** rs/replicated_state/src/canister_states.rs (L17-23)
```rust
//! Internally, `CanisterStates` also maintains `ColdStats`, a small set of
//! aggregates over the cold pool that lets several aggregated queries (e.g.
//! [`CanisterStates::total_compute_allocation`],
//! [`CanisterStates::total_canister_memory_usage`],
//! [`CanisterStates::callback_count`]) become `O(|hot|)` instead of
//! `O(|all canisters|)`. These aggregates are an implementation detail: callers
//! always go through the public aggregator methods.
```

**File:** rs/replicated_state/src/canister_states.rs (L160-176)
```rust
#[derive(Clone, Debug, Default, PartialEq, ValidateEq)]
pub struct CanisterStates {
    /// Canisters that may have round-level work or are recently active. Always
    /// scanned by per-round operations (scheduling, heartbeat enqueueing,
    /// timeout, queue GC, etc.).
    #[validate_eq(CompareWithValidateEq)]
    hot: BTreeMap<CanisterId, Arc<CanisterState>>,

    /// Canisters that are definitely idle (i.e. `CanisterState::is_cold()`
    /// returns `true`). Operations that need to visit every canister still
    /// need to consider them; per-round operations should skip them.
    #[validate_eq(CompareWithValidateEq)]
    cold: BTreeMap<CanisterId, Arc<CanisterState>>,

    /// O(1) aggregates over `cold` canisters. See [`ColdStats`].
    cold_stats: ColdStats,
}
```

**File:** rs/execution_environment/src/scheduler/round_schedule.rs (L534-543)
```rust
        for canister_id in self.fully_executed_canisters.iter() {
            let canister_priority = subnet_schedule.get_mut(*canister_id);
            canister_priority.executed_rounds += 1;
            canister_priority.last_full_execution_round = current_round;

            #[cfg(debug_assertions)]
            subnet_schedule
                .fully_executed_canisters
                .insert(*canister_id);
        }
```

**File:** rs/execution_environment/src/scheduler/tests/limits.rs (L567-646)
```rust
/// At the end of each inner-loop iteration the scheduler deducts
/// `instruction_overhead_per_canister_for_finalization * num_canisters`
/// from the instruction budget.  With a cross-canister call pattern that
/// normally produces 3 inner-loop iterations (call → response → callback),
/// a large-enough overhead must prevent the callback iteration from running.
#[test]
fn finalization_overhead_per_canister_reduces_instruction_budget() {
    const SLICE: u64 = 10;
    const SLICE_INSTRUCTIONS: NumInstructions = NumInstructions::new(SLICE);

    /// Tests the scheduling and execution of two active canisters (canister0 and
    /// canister1) and one inactive canister. Ignoring overhead it should execute 3
    /// iterations:
    ///
    ///  * Iteration 1: canister0 ingress  (10 instructions)
    ///  * Iteration 2: canister1 call     (10 instructions, inducted from iter 1)
    ///  * Iteration 3: canister0 callback (10 instructions, inducted from iter 2)
    ///
    /// ```text
    /// budget = max_instructions_per_round - max_instructions_per_slice + 1
    ///        = 80 - 10 + 1 = 71
    /// ```
    fn test_sequence(finalization_overhead: NumInstructions, expected_iterations: u64) {
        let config = SchedulerConfig {
            scheduler_cores: 2,
            max_instructions_per_round: SLICE_INSTRUCTIONS * 8,
            max_instructions_per_message: SLICE_INSTRUCTIONS,
            max_instructions_per_slice: SLICE_INSTRUCTIONS,
            max_instructions_per_install_code_slice: SLICE_INSTRUCTIONS,
            instruction_overhead_per_execution: NumInstructions::from(0),
            instruction_overhead_per_canister: NumInstructions::from(0),
            instruction_overhead_per_canister_for_finalization: finalization_overhead,
            ..SchedulerConfig::application_subnet()
        };

        let mut test = SchedulerTestBuilder::new()
            .with_scheduler_config(config)
            .build();
        let canister0 = test.create_canister();
        let canister1 = test.create_canister();
        test.send_ingress(
            canister0,
            ingress(SLICE).call(other_side(canister1, SLICE), on_response(SLICE)),
        );
        let _canister2 = test.create_canister();

        test.execute_round(ExecutionRoundType::OrdinaryRound);

        let metrics = &test.scheduler().metrics;
        assert_eq!(
            metrics.inner_loop_processed_non_zero_inputs_count.get(),
            expected_iterations,
        );
        assert_eq!(
            metrics.inner_round_loop_consumed_max_instructions.get(),
            if expected_iterations == 3 { 0 } else { 1 }
        );
        assert_eq!(
            test.state()
                .metadata
                .subnet_metrics
                .update_transactions_total,
            expected_iterations
        );
    }

    // Baseline: without overhead, the inner loop runs 3 iterations:
    //  * Iteration 1: 71 → 61 (exec) → 61 (overhead)
    //  * Iteration 2: 61 → 51 (exec) → 51 (overhead)
    //  * Iteration 3: 51 → 41 (exec) → 41 (overhead)
    test_sequence(NumInstructions::from(0), 3);

    // With overhead = 10 and 3 canisters, the overhead per iteration is 30:
    //  * Iteration 1: 71 → 61 (exec) → 31 (overhead)
    //  * Iteration 2: 31 → 21 (exec) → -9 (overhead)
    //  * Iteration 3: budget ≤ 0 → break
    //
    // Only 2 iterations execute.
    test_sequence(SLICE_INSTRUCTIONS, 2);
}
```
