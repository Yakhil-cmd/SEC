Audit Report

## Title
Stale Voting Power Snapshot Used for Ballot Creation Allows Dissolved Neurons to Earn Inflated Rewards - (File: `rs/nns/governance/src/governance/voting_power_snapshots.rs`)

## Summary

When a voting power spike is detected at proposal creation time, NNS Governance initializes ballots from a stale historical snapshot rather than current neuron state. Because `SnapshotVotingPowerTask` freezes the snapshot store whenever the latest snapshot is itself a spike, an attacker can engineer a sustained spike condition, dissolve a neuron (withdrawing its ICP) while the store is frozen, then trigger proposal creation — receiving a ballot with the old (higher) voting power and earning maturity rewards proportional to stake they no longer hold. The maturity is subsequently minted as ICP via `DisburseMaturity`, constituting a ledger conservation violation.

## Finding Description

**Snapshot freeze during spike:** `SnapshotVotingPowerTask::execute` at `rs/nns/governance/src/timer_tasks/snapshot_voting_power.rs` L35–40 calls `is_latest_snapshot_a_spike` and returns early without recording a new snapshot if the latest snapshot is already a spike. This means a sustained spike condition permanently freezes the snapshot store at its current state.

**Stale snapshot selection:** `VotingPowerSnapshots::totals_entry_with_minimum_total_potential_voting_power_if_voting_power_spiked` at `rs/nns/governance/src/governance/voting_power_snapshots.rs` L130–137 selects the snapshot with the minimum `total_potential_voting_power` among all non-stale entries (filtered by `MAXIMUM_STALENESS_SECONDS = ONE_MONTH_SECONDS * 3`). This snapshot can be up to 3 months old if the store has been frozen.

**Ballot creation from stale snapshot:** `compute_ballots_for_standard_proposal` at `rs/nns/governance/src/governance.rs` L5506–5524 uses the stale snapshot's per-neuron voting power map to initialize ballots when a spike is detected. No reconciliation against current neuron state is performed.

**Reward calculation from ballot VP without current-state check:** `calculate_voting_rewards` at `rs/nns/governance/src/governance.rs` L6696–6743 calls `sum_weighted_voting_power` (in `rs/nns/governance/src/proposals/mod.rs` L351–373), which reads `ballot.voting_power` directly. The only guard is `self.neuron_store.contains(neuron_id)` — existence, not current stake. No check verifies that the neuron's current stake is consistent with the ballot's voting power.

**ICP minting from maturity:** `try_finalize_maturity_disbursement` at `rs/nns/governance/src/governance/disburse_maturity.rs` L630–643 mints ICP from the governance canister's minting account proportional to accumulated maturity, with no reference to the neuron's current stake.

**Exploit path (with Mission 70 enabled, 2-week minimum dissolve delay):**

1. Attacker holds neuron N in dissolving state with dissolve delay = 14 days + 1 second. Snapshot taken at T=0 captures neuron N with VP_old.
2. At T=0 + 1 second, attacker stakes a large amount in a new neuron, pushing current total VP above 1.5× the minimum snapshot total.
3. `SnapshotVotingPowerTask` detects the spike at every subsequent daily execution and skips recording — the store is frozen at T=0.
4. At T=14 days + 1 second, neuron N's dissolve delay reaches 0. Attacker disburses neuron N, withdrawing its ICP.
5. Attacker (or any user) creates a standard proposal. `compute_ballots_for_standard_proposal` detects the spike and calls `previous_ballots_if_voting_power_spike_detected`, which returns the T=0 snapshot (age = 14 days < 3 months, not filtered). Neuron N receives a ballot with VP_old despite holding 0 ICP.
6. Attacker votes on the proposal using neuron N's ballot (neuron still exists, just with 0 stake).
7. `distribute_voting_rewards_to_neurons` credits neuron N with maturity proportional to VP_old. Attacker calls `DisburseMaturity`, minting ICP not backed by any current stake.

Without Mission 70, the same path applies for any neuron whose remaining dissolve delay at snapshot time is less than `MAXIMUM_STALENESS_SECONDS` (3 months), i.e., a neuron in dissolving state with < 3 months remaining.

## Impact Explanation

This is a **ledger conservation violation in NNS governance**: ICP is minted via `DisburseMaturity` in amounts proportional to voting power that is no longer backed by staked ICP. An unprivileged neuron owner can earn maturity rewards on stake they have already withdrawn. The maturity is real ICP upon disbursement. This matches the allowed impact: "Significant NNS security impact with concrete user or protocol harm" (High, $2,000–$10,000), or potentially "Theft, permanent loss, illegal minting" (Critical) if the scale is large enough, though practical constraints limit the per-exploit gain.

## Likelihood Explanation

**Medium.** The primary constraint is triggering a 1.5× spike in total NNS voting power, which requires staking a very large amount of ICP (on the order of the current total staked supply). However, the attacker does not need to trigger the spike themselves — they can wait for organic network growth or a large staking event to cause a spike, then time their neuron dissolution to coincide. The 3-month staleness window and the spike-freeze behavior make the exploitation window potentially long. With Mission 70 enabled (2-week minimum dissolve delay), the required neuron dissolution window is short and practical. No privileged access is required.

## Recommendation

1. **Do not freeze the snapshot store during a spike.** Remove the early-return in `SnapshotVotingPowerTask::execute` when `is_latest_snapshot_a_spike` returns true. New snapshots should always be recorded so the store reflects current state.

2. **Validate ballot voting power against current neuron state at reward distribution time.** In `calculate_voting_rewards` / `sum_weighted_voting_power`, cap each neuron's effective voting power for reward purposes to its current `deciding_voting_power` or `potential_voting_power`, not the stale ballot value.

3. **Reduce `MAXIMUM_STALENESS_SECONDS`.** The current 3-month limit allows very old snapshots to be used. Reducing this to match the snapshot window (e.g., 7–14 days) would limit the staleness of ballots.

4. **At reward settlement, skip or reduce rewards for neurons whose current stake is materially lower than their ballot voting power.** In `distribute_voting_rewards_to_neurons`, before crediting maturity, verify the neuron's current stake is consistent with its ballot voting power and reduce proportionally if not.

## Proof of Concept

**Deterministic integration test plan (PocketIC):**

1. Create governance with Mission 70 enabled (2-week minimum dissolve delay).
2. Add neuron N with `cached_neuron_stake_e8s = 10_000 * E8` and dissolve delay = 14 days + 1 second (dissolving state).
3. Run `SnapshotVotingPowerTask` 7 times (one per day) to fill the snapshot store. Verify neuron N appears in all 7 snapshots with VP_old.
4. Add a new neuron M with `cached_neuron_stake_e8s` large enough to push current total VP above 1.5× the minimum snapshot total.
5. Advance time by 1 day. Run `SnapshotVotingPowerTask`. Assert that `latest_snapshot_timestamp_seconds` has NOT advanced (spike detected, recording skipped).
6. Advance time by 14 days. Disburse neuron N (withdraw ICP). Assert neuron N has 0 stake.
7. Create a standard proposal. Assert that the returned ballots include neuron N with `voting_power = VP_old` (from the stale snapshot).
8. Vote on the proposal with neuron N.
9. Advance time past the reward distribution period. Call `distribute_voting_rewards_to_neurons`.
10. Assert that neuron N has `maturity_e8s_equivalent > 0` despite having 0 staked ICP.
11. Call `DisburseMaturity` on neuron N. Assert that ICP is minted to the attacker's account.
12. Verify total ICP minted exceeds what is justified by neuron N's current (zero) stake.