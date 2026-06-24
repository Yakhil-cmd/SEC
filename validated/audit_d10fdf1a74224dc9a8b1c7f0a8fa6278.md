Audit Report

## Title
Unbounded Loop Over Proposals × Ballots in SNS `distribute_rewards` Causes Permanent Instruction-Limit DoS — (File: `rs/sns/governance/src/governance.rs`)

## Summary
The SNS governance canister's `distribute_rewards` function executes five sequential unbounded loops in a single synchronous message with no instruction-limit guard anywhere in the function. If the combined work exceeds the IC per-message instruction limit, the message traps, all state changes are rolled back, `latest_reward_event` is not updated, and every subsequent periodic-task invocation encounters the same (or larger) workload — permanently preventing reward distribution and proposal settlement without a canister upgrade.

## Finding Description
`distribute_rewards` at `rs/sns/governance/src/governance.rs:5763` performs the following unbounded sequential loops in one synchronous execution:

1. **`for i in 1..=new_rounds_count`** (L5861) — iterates over every missed reward round since the last distribution.
2. **`for proposal_id in &considered_proposals`** (L5894) — iterates over every proposal in `ReadyToSettle` state.
3. **`for (voter, ballot) in &proposal.ballots`** (L5896) — for each such proposal, iterates over every ballot (one per eligible neuron at proposal creation time), performing `NeuronId::from_str`, Decimal arithmetic, and HashMap insertion per entry.
4. **`for (neuron_id, neuron_reward_shares) in neuron_id_to_reward_shares`** (L5954) — iterates over every neuron that voted across all settled proposals, performing mutable neuron lookup and Decimal arithmetic per entry.
5. **`for pid in &considered_proposals`** (L6013) — iterates over all settled proposals again, calling `self.process_proposal(pid.id)` and clearing ballots.

A `grep` search for `is_message_over_threshold`, `is_over_instructions_limit`, or any instruction-limit check in `rs/sns/governance/src/governance.rs` returns **no matches** — confirming the complete absence of any guard.

The `latest_reward_event` is only written at the very end of the function (L6084–6092). If the message traps at any point before that, the IC rolls back all state changes. Because `considered_proposals` is re-collected from scratch on every invocation (L5822–5823) and proposals are only marked settled at the end (L6013–6081), the next timer invocation processes the same set of proposals plus any newly accumulated ones — making the DoS self-reinforcing.

By contrast, NNS governance was explicitly refactored: `rs/nns/governance/src/reward/distribution.rs:154–188` implements `RewardsDistribution::continue_processing` with an `is_over_instructions_limit` callback that breaks the loop when the soft limit is reached, and `rs/nns/governance/src/timer_tasks/distribute_rewards.rs:43–55` schedules this as a `PeriodicSyncTask` running every 2 seconds to resume across multiple messages. The SNS governance canister received no equivalent fix.

## Impact Explanation
This matches the allowed ICP bounty impact: **"Application/platform-level DoS … or subnet availability impact not based on raw volumetric DDoS" — High ($2,000–$10,000)**.

Concretely:
- Voting rewards are never distributed to SNS neurons.
- Proposals in `ReadyToSettle` state are never settled; their ballots are never cleared, causing unbounded state growth.
- The accumulation of unsettled proposals and ballots over time makes the workload monotonically worse, making recovery impossible without a canister upgrade.
- Other periodic tasks in `run_periodic_tasks` (maturity finalization, GC, etc.) that execute after `distribute_rewards` may also be disrupted if the async call to `ledger.total_supply()` completes but the subsequent synchronous `distribute_rewards` traps.

## Likelihood Explanation
**Low-to-Medium.** No privileged access is required. Any SNS token holder can stake to create neurons; any neuron with sufficient stake can submit proposals. The total work scales as `P × N` (proposals × neurons). For a popular SNS with thousands of neurons and dozens of accumulated `ReadyToSettle` proposals, the threshold can be reached organically. A motivated attacker holding SNS tokens can accelerate this by creating many neurons and submitting many proposals. The attack is repeatable: once triggered, every subsequent timer invocation re-triggers the trap without any attacker action.

## Recommendation
Apply the same batching pattern already implemented in NNS governance:
1. Introduce a persistent `RewardsDistributionInProgress` state (analogous to NNS's `RewardsDistribution`) that survives across messages.
2. Replace the synchronous `distribute_rewards` with a state machine that saves intermediate progress and resumes across timer invocations.
3. Add `is_over_instructions_limit` checks inside the ballot and neuron-reward loops, breaking and persisting state when the soft limit is reached.
4. Schedule a dedicated periodic timer (analogous to `DistributeRewardsTask` at 2-second intervals) to continue processing until the distribution is complete.
5. As a short-term mitigation, enforce a hard cap on the number of proposals that can simultaneously be in `ReadyToSettle` state.

## Proof of Concept
**Setup:**
- Deploy an SNS with `round_duration_seconds` set to a short interval (e.g., 86400 seconds).
- Create N neurons (e.g., 10,000–50,000) by having token holders stake.
- Submit P proposals (e.g., 50–200) and allow them to reach `ReadyToSettle` state (voting period expires).

**Trigger:**
- Wait for the periodic task timer to fire and invoke `run_periodic_tasks` → `distribute_rewards`.
- The inner ballot loop at L5896 processes N × P entries. At realistic instruction costs per entry (NeuronId string parsing, Decimal arithmetic, HashMap operations), the total instruction count approaches or exceeds the IC per-message limit.

**Verification (deterministic integration test):**
- Write a PocketIC or state-machine test that creates an SNS with 10,000 neurons, submits 100 proposals, advances time past their voting period, and then calls `run_periodic_tasks`.
- Assert that `latest_reward_event` is **not** updated (trap occurred and state was rolled back).
- Assert that on the next invocation, `latest_reward_event` is still not updated (permanent DoS confirmed).

**Root cause confirmation:** The absence of any instruction-limit guard is confirmed at `rs/sns/governance/src/governance.rs:5763–6093` — the entire function body contains no call to any instruction-counting or limit-checking API, in contrast to the NNS fix at `rs/nns/governance/src/reward/distribution.rs:184`.