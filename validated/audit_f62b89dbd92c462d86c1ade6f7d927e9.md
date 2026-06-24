Audit Report

## Title
Unbounded Ballot Iteration in SNS Governance `distribute_rewards` Exhausts Instruction Limit, Permanently Halting Reward Distribution - (File: `rs/sns/governance/src/governance.rs`)

## Summary
The `distribute_rewards` function in SNS governance iterates over all ballots of all `ReadyToSettle` proposals and all voting neurons in a single synchronous execution with no instruction-limit guard. At ceiling configuration (200,000 neurons × 700 proposals), the ballot aggregation loop alone can reach 140 million iterations. When the IC instruction limit is exceeded inside the timer callback, state is rolled back and the persistent condition causes every subsequent invocation to reproduce the same trap, permanently halting voting reward distribution for the SNS.

## Finding Description
`distribute_rewards` (`rs/sns/governance/src/governance.rs`, line 5763) is called synchronously from `run_periodic_tasks` (line 5513), which is scheduled as a repeating timer (canister.rs line 632). It contains two unbounded loops with no instruction-limit check:

**Loop 1 — ballot aggregation (lines 5894–5930):**
```rust
for proposal_id in &considered_proposals {
    if let Some(proposal) = self.get_proposal_data(*proposal_id) {
        for (voter, ballot) in &proposal.ballots {
            // NeuronId::from_str + HashMap entry + Decimal arithmetic
        }
    }
}
```
`considered_proposals` can hold up to `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS_CEILING = 700` entries; each proposal's ballot map can hold up to `MAX_NUMBER_OF_NEURONS_CEILING = 200,000` entries. Worst-case: 700 × 200,000 = 140 million iterations, each performing `NeuronId::from_str`, a `HashMap` entry operation, and `Decimal` arithmetic — far exceeding the 5B instruction limit.

**Loop 2 — maturity distribution (lines 5954–5997):**
```rust
for (neuron_id, neuron_reward_shares) in neuron_id_to_reward_shares {
    let neuron: &mut Neuron = match self.get_neuron_result_mut(&neuron_id) { ... };
    // Decimal division + neuron mutation
}
```
Up to 200,000 neurons, each requiring a mutable neuron lookup and `Decimal` arithmetic, with no instruction-limit break.

A grep search across all of `rs/sns/governance/` confirms there is no `is_message_over_threshold`, `DISTRIBUTION_MESSAGE_LIMIT`, or any instruction-limit guard anywhere in the SNS governance codebase.

By contrast, NNS governance (`rs/nns/governance/src/reward/distribution.rs`, line 184) explicitly breaks the distribution loop using `is_message_over_threshold(DISTRIBUTION_MESSAGE_LIMIT)` and processes rewards across multiple timer invocations via `RewardsDistributionStateMachine`. The SNS governance has received no equivalent fix.

The constants are confirmed in `rs/sns/governance/src/types.rs` lines 386 and 390:
- `MAX_NUMBER_OF_NEURONS_CEILING: u64 = 200_000`
- `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS_CEILING: u64 = 700`

## Impact Explanation
When the IC instruction limit is exceeded inside a timer callback, execution traps and all state mutations are rolled back. Because the condition that caused the overflow (large neuron count + large ballot count) is persistent state, every subsequent timer invocation reproduces the same trap. The SNS governance canister enters a permanent liveness failure: voting rewards are never distributed, neuron maturity never increases, and `latest_reward_event` is never updated. This is a concrete, permanent application-level DoS of the SNS reward mechanism, matching the allowed impact: **High ($2,000–$10,000) — Significant SNS security impact with concrete user or protocol harm**, and also **High — Application/platform-level DoS not based on raw volumetric DDoS**.

## Likelihood Explanation
Any SNS configured with `max_number_of_neurons` and `max_number_of_proposals_with_ballots` near their ceiling values is at risk. These are governance-controlled parameters that an SNS community may raise to support growth. Once set, an unprivileged participant who creates neurons and votes on proposals — normal, incentivized behavior — organically drives the system toward the failure threshold. No special privilege, coordination, or attack tooling is required beyond normal participation at scale.

## Recommendation
Apply the same pattern already used in NNS governance:
1. Move per-neuron maturity increment out of `distribute_rewards` into a separate batched timer task analogous to `RewardsDistributionStateMachine` in NNS.
2. Store the pending per-neuron reward map in stable memory after `distribute_rewards` computes the ballot aggregation.
3. Process the map in chunks across multiple timer invocations, checking `is_message_over_threshold` after each neuron update and breaking when the limit is approached.
4. Cap `new_rounds_count` to a safe maximum per invocation, or initialize `reward_start_timestamp_seconds` from `genesis_timestamp_seconds` rather than defaulting to Unix epoch 0.

## Proof of Concept
1. Deploy an SNS with `max_number_of_neurons = 200_000` and `max_number_of_proposals_with_ballots = 700`.
2. Create 200,000 neurons (achievable by any token holder staking the minimum stake).
3. Submit 700 proposals and have all neurons vote on each (via following, this requires only the root neuron to vote explicitly).
4. Wait for `run_periodic_tasks` to fire (every `RUN_PERIODIC_TASKS_INTERVAL`).
5. `should_distribute_rewards` returns `true`; `distribute_rewards` is called.
6. The ballot aggregation loop at line 5894 iterates 700 × 200,000 = 140 million times, each calling `NeuronId::from_str` and a `HashMap` entry operation.
7. The IC instruction counter exceeds 5 billion; the timer callback traps; state is rolled back.
8. `latest_reward_event` is never updated; `should_distribute_rewards` returns `true` again on the next tick.
9. The SNS governance canister is permanently unable to distribute voting rewards.

A deterministic integration test using PocketIC can reproduce this by populating the SNS state with 200,000 neurons and 700 proposals with full ballots, then advancing the timer and asserting that `latest_reward_event` is never updated after repeated `run_periodic_tasks` invocations.