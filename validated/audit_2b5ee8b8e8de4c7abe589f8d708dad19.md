Audit Report

## Title
Unbounded Single-Message Reward Distribution Loop in SNS Governance Can Permanently DOS Periodic Tasks - (File: rs/sns/governance/src/governance.rs)

## Summary
The SNS governance `distribute_rewards` function executes three unbounded loops synchronously in a single IC message with no instruction-limit guard. At the parameter ceilings of 200,000 neurons and 700 proposals, the ballot aggregation loop alone can process up to 140 million entries, exceeding the IC per-message instruction limit. Because `latest_reward_event` is only written at the very end of the function, any trap causes a full state rollback, and every subsequent timer invocation re-attempts and re-traps, permanently halting reward distribution.

## Finding Description
`distribute_rewards` is called synchronously from `run_periodic_tasks` at line 5513 with no `await` points and no instruction-limit guard. The function contains three unbounded loops:

**Loop 1 — ballot aggregation** (lines 5894–5931): iterates over all `considered_proposals` (up to 700) and all ballots in each proposal (up to 200,000 per proposal), performing `NeuronId::from_str` and `Decimal` arithmetic per entry — up to 140 million iterations.

**Loop 2 — neuron maturity update** (lines 5954–5997): iterates over all entries in `neuron_id_to_reward_shares` (up to 200,000 neurons), with a `panic!` at line 5978 that is itself a trap path.

**Loop 3 — proposal settlement** (lines 6013–6081): iterates over all `considered_proposals`, calling `self.process_proposal` per entry.

`latest_reward_event` is only written at lines 6084–6092, the very last statement of the function. If the function traps at any point before that line (instruction limit exceeded, or the `panic!` in Loop 2), the IC rolls back all state changes. `should_distribute_rewards()` then sees the unchanged `latest_reward_event` and schedules another call to `distribute_rewards`, which traps again. The cycle repeats indefinitely.

A grep for `is_message_over_threshold` in `rs/sns/governance/` returns no matches, confirming the absence of any batching or instruction-limit guard. The NNS governance canister has already addressed this exact class of bug via `RewardsDistributionStateMachine` and `distribute_pending_rewards` in `rs/nns/governance/src/reward/distribution.rs` (lines 42–52), which checks `is_message_over_threshold` after each neuron and resumes in the next message. SNS governance has received no equivalent fix.

The parameter ceilings bounding the work are confirmed at `rs/sns/governance/src/types.rs` lines 386 and 390: `MAX_NUMBER_OF_NEURONS_CEILING = 200_000` and `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS_CEILING = 700`.

## Impact Explanation
Reward distribution is permanently halted for the affected SNS. Neurons that voted on proposals never receive maturity rewards. The `run_periodic_tasks` timer continues to fire but always traps at `distribute_rewards`, consuming subnet resources and blocking all subsequent work in the same timer callback (maturity finalization, metrics, GC). The SNS governance canister cannot self-recover without an upgrade. This matches the allowed impact: **High — Significant SNS security impact with concrete user and protocol harm**, and **High — Application/platform-level DoS not based on raw volumetric DDoS**.

## Likelihood Explanation
An unprivileged SNS participant can trigger this by staking tokens to create neurons up to `max_number_of_neurons`, submitting proposals up to `max_number_of_proposals_with_ballots`, having all neurons vote on each, and waiting for the reward round to end. No privileged access, governance majority, or threshold attack is required. The condition can also arise organically in SNS instances with high participation and proposal throughput, without adversarial intent.

## Recommendation
Apply the same batching pattern already used in NNS governance:
1. After calculating `neuron_id_to_reward_shares`, persist the pending distribution to stable storage (analogous to `RewardsDistributionStateMachine`) and update `latest_reward_event` immediately to prevent re-entry.
2. Process neuron maturity updates in a separate periodic timer task that checks `is_message_over_threshold` after each neuron and resumes in the next message if the limit is reached.
3. Add an instruction-limit guard to the ballot-aggregation loop (Loop 1), or cap `considered_proposals` to a safe batch size per invocation.

## Proof of Concept
1. Deploy an SNS with `max_number_of_neurons = 200_000` and `max_number_of_proposals_with_ballots = 700`.
2. Create 200,000 neurons (each staking the minimum).
3. Submit 700 proposals and have all 200,000 neurons vote on each.
4. Wait for the reward round to end (`should_distribute_rewards()` returns `true`).
5. Observe that the next `run_periodic_tasks` timer invocation traps (instruction limit exceeded in `distribute_rewards` at the ballot aggregation loop, lines 5894–5931).
6. Observe that `latest_reward_event` is unchanged (state rolled back — line 6084 was never reached).
7. Observe that every subsequent timer invocation also traps — reward distribution is permanently halted.

A deterministic integration test using PocketIC can reproduce this by populating the governance proto with 200,000 neurons and 700 proposals each with 200,000 ballots, advancing the clock past the reward round boundary, and asserting that `run_periodic_tasks` traps and `latest_reward_event.round` remains unchanged across repeated invocations.