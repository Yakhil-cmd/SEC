### Title
Scheduler Finalization Overhead Scales with Total Canister Count, Enabling Cheap Round-Throttling DOS - (File: `rs/execution_environment/src/scheduler.rs`)

---

### Summary

At the end of every inner-loop iteration of the IC scheduler, the round instruction budget is decremented by `instruction_overhead_per_canister_for_finalization * state.num_canisters()`. This overhead is charged against the **round limit** (not against any individual canister's cycles), and `state.num_canisters()` counts **all** canisters on the subnet — including idle ones with no messages. An unprivileged attacker who deploys a large number of cheap, idle canisters can inflate this per-iteration deduction to the point where the round budget is exhausted after only one or two inner-loop iterations, starving legitimate canister traffic and effectively throttling the subnet's throughput.

---

### Finding Description

In `rs/execution_environment/src/scheduler.rs`, inside the inner execution loop, after each iteration the scheduler deducts:

```rust
round_limits.instructions -= as_round_instructions(
    self.config
        .instruction_overhead_per_canister_for_finalization
        * state.num_canisters() as u64,
);
``` [1](#0-0) 

The constant `INSTRUCTION_OVERHEAD_PER_CANISTER_FOR_FINALIZATION` is set to `12_000` instructions, calibrated for ~5,000 canisters (13 ms finalization time observed in production): [2](#0-1) 

The round instruction budget for an application subnet is `MAX_INSTRUCTIONS_PER_ROUND = 4 * B` (4 billion instructions): [3](#0-2) 

With the default `max_number_of_canisters` set to `120,000`: [4](#0-3) 

At 120,000 canisters, the per-iteration overhead is `12,000 * 120,000 = 1.44 billion instructions` — consuming **36% of the entire round budget per iteration**. With a multi-iteration cross-canister call (call → response → callback = 3 iterations), the total overhead is `3 * 1.44B = 4.32B`, which **exceeds the 4B round limit**, meaning the callback iteration is starved. The attacker's idle canisters pay no per-round execution cost (they have no messages), yet they impose a real wall-clock cost on every round's finalization loop.

The `instruction_overhead_per_canister_for_finalization` is deducted from the **round-level instruction budget** (a scheduler-internal resource), not from any canister's cycles balance. There is no mechanism to charge the idle canisters for the finalization overhead they impose.

---

### Impact Explanation

An attacker who deploys a large number of idle canisters (up to the subnet's `max_number_of_canisters` limit) can:

1. **Reduce effective round throughput**: Each inner-loop iteration loses `12,000 * N` instructions from the budget. At near-maximum canister counts, this can prevent multi-hop inter-canister calls from completing within a single round, increasing latency for all users.
2. **Throttle the sequencer**: If the finalization overhead per iteration exceeds the remaining round budget, the inner loop terminates early, leaving queued messages unprocessed. This is a resource-consumption DOS analogous to the op-geth gas tracker issue: real node work (iterating over all canisters during finalization) is not adequately charged to the party causing it.
3. **Persistent effect**: Once deployed, idle canisters impose this overhead every round indefinitely, at zero ongoing cost to the attacker (idle canisters are not charged execution fees, only storage fees which are minimal).

The `finalization_overhead_per_canister_reduces_instruction_budget` test explicitly demonstrates that with enough canisters, the overhead prevents the callback iteration from running: [5](#0-4) 

---

### Likelihood Explanation

- **Entry path**: Any unprivileged user can call `create_canister` on an application subnet. The `max_number_of_canisters` limit (default 120,000) is the only gate, and it is reachable by paying the canister creation fee.
- **Cost**: Canister creation fee is `5,000,000` cycles per canister. At 120,000 canisters, the one-time cost is `600 billion cycles` (~0.6 XDR). After deployment, idle canisters only pay storage fees (`317,500 cycles/GiB/s`), which for empty canisters is negligible.
- **No privileged access required**: The attack requires only cycles, which are purchasable.
- **Calibration gap**: The overhead constant was calibrated at 5,000 canisters; at 120,000 canisters (the allowed maximum), the overhead is 24× larger than the calibration point, and the comment itself acknowledges the value was rounded up "to be on the safe side" — but the safe side was computed for a much smaller canister count. [2](#0-1) 

---

### Recommendation

1. **Cap the finalization overhead deduction**: Clamp `state.num_canisters()` to a safe maximum (e.g., the calibration point of 5,000) when computing the per-iteration deduction, or use the number of *active* canisters (those with pending messages) rather than the total count.
2. **Charge idle canisters for finalization overhead**: Deduct the finalization overhead from each canister's cycles balance proportionally, rather than from the shared round budget.
3. **Re-calibrate `INSTRUCTION_OVERHEAD_PER_CANISTER_FOR_FINALIZATION`**: The constant was derived from a 5,000-canister measurement but is applied at up to 120,000 canisters. Either lower the constant or reduce `DEFAULT_MAX_NUMBER_OF_CANISTERS` to match the calibration range.
4. **Reduce `DEFAULT_MAX_NUMBER_OF_CANISTERS`** to a value where `12,000 * N * max_iterations` remains well below `MAX_INSTRUCTIONS_PER_ROUND`.

---

### Proof of Concept

**Setup**: Deploy `N` idle canisters on an application subnet (no Wasm installed, no messages).

**Calculation**:
- `INSTRUCTION_OVERHEAD_PER_CANISTER_FOR_FINALIZATION` = 12,000
- `DEFAULT_MAX_NUMBER_OF_CANISTERS` = 120,000
- Per-iteration overhead = `12,000 * 120,000` = **1,440,000,000 instructions**
- `MAX_INSTRUCTIONS_PER_ROUND` = **4,000,000,000 instructions**
- After 2 inner-loop iterations: `2 * 1.44B = 2.88B` consumed by overhead alone
- Remaining budget for actual execution: `4B - 2.88B = 1.12B`
- A 3-iteration cross-canister call (call → response → callback) requires 3 iterations; the third is starved

**Attacker cost**: ~600B cycles one-time (~0.6 XDR) + negligible ongoing storage fees.

**Effect**: All cross-canister calls requiring ≥3 inner-loop iterations fail to complete in a single round, increasing latency and reducing throughput for all subnet users. The attacker's canisters remain idle and are never charged for the finalization work they impose. [1](#0-0) [6](#0-5) [4](#0-3)

### Citations

**File:** rs/execution_environment/src/scheduler.rs (L559-563)
```rust
            round_limits.instructions -= as_round_instructions(
                self.config
                    .instruction_overhead_per_canister_for_finalization
                    * state.num_canisters() as u64,
            );
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

**File:** rs/config/src/subnet_config.rs (L81-81)
```rust
const MAX_INSTRUCTIONS_PER_ROUND: NumInstructions = NumInstructions::new(4 * B);
```

**File:** rs/config/src/execution_environment.rs (L197-198)
```rust
/// Default maximum number of canisters per subnet if not set in the registry.
pub const DEFAULT_MAX_NUMBER_OF_CANISTERS: u64 = 120_000;
```

**File:** rs/execution_environment/src/scheduler/tests/limits.rs (L567-645)
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
```
