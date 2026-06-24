### Title
SNS Governance Voting Power Snapshot at Proposal Submission Time Allows Stake Manipulation Before Ballot Freeze - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS governance canister snapshots each neuron's voting power at the exact moment a proposal is submitted (`make_proposal`), with no prior periodic snapshot mechanism analogous to the NNS's `SnapshotVotingPowerTask`. An unprivileged SNS token holder can observe a pending proposal submission (or submit one themselves), then race to stake additional tokens or increase their dissolve delay in the same round to inflate their ballot voting power before the snapshot is taken. This is the direct IC analog of the UMA "token holders can react to revealed vote" class: the stake/voting-power snapshot is taken at a single, predictable, user-triggerable moment rather than being pre-committed.

---

### Finding Description

In the NNS governance canister, a `SnapshotVotingPowerTask` runs daily and records periodic `VotingPowerSnapshot` entries in `VOTING_POWER_SNAPSHOTS`. When a standard proposal is submitted, `compute_ballots_for_standard_proposal` checks whether the current total potential voting power constitutes a spike relative to those pre-committed snapshots; if so, it falls back to the historical minimum snapshot. This prevents a proposer from inflating their own voting power at proposal time.

The SNS governance canister has **no equivalent periodic snapshot mechanism**. Its `compute_ballots_for_new_proposal` is called directly inside `make_proposal` and reads live neuron state at `now_seconds` with no historical baseline:

```rust
// rs/sns/governance/src/governance.rs:5255-5279
for (k, v) in self.proto.neurons.iter() {
    if v.dissolve_delay_seconds(now_seconds) < min_dissolve_delay_for_vote {
        continue;
    }
    let voting_power = v.voting_power(
        now_seconds,
        max_dissolve_delay,
        max_age_bonus,
        max_dissolve_delay_bonus_percentage,
        max_age_bonus_percentage,
    );
    electoral_roll.insert(k.clone(), Ballot { vote: Vote::Unspecified as i32, voting_power, cast_timestamp_seconds: 0 });
}
```

The voting power formula includes `dissolve_delay_seconds` and `cached_neuron_stake_e8s` as live inputs. Both can be changed by the neuron owner in the same block/round as the proposal submission:

- **Stake increase**: A neuron owner can transfer SNS tokens to their neuron's subaccount and call `claim_or_refresh_neuron` to update `cached_neuron_stake_e8s` immediately before `make_proposal` is processed.
- **Dissolve delay increase**: A neuron owner can call `manage_neuron` with `IncreaseDissolveDelay` immediately before `make_proposal`.

Because the IC processes ingress messages sequentially within a round, and because the SNS governance canister is a single canister with no cross-round snapshot, an attacker who controls the proposal submission (or who can observe a pending proposal in the ingress queue) can front-run the snapshot by submitting neuron-modification messages first.

The NNS explicitly guards against this with `compute_ballots_for_standard_proposal` + `VOTING_POWER_SNAPSHOTS`:

```rust
// rs/nns/governance/src/governance.rs:5506-5512
let maybe_previous_ballots_if_voting_power_spike_detected = VOTING_POWER_SNAPSHOTS
    .with_borrow(|snapshots| {
        snapshots.previous_ballots_if_voting_power_spike_detected(
            current_voting_power_snapshot.total_potential_voting_power(),
            now_seconds,
        )
    });
```

No such guard exists in the SNS path.

---

### Impact Explanation

An attacker who controls a neuron (or coordinates with one) can:

1. Temporarily acquire a large amount of SNS tokens (e.g., via a flash-loan-equivalent on a DEX, or by pre-purchasing).
2. Stake them into their neuron immediately before submitting a proposal (or before another user's proposal is processed).
3. Receive an inflated ballot voting power on that proposal.
4. Vote on the proposal with disproportionate weight.
5. Unstake after the voting period ends (subject to dissolve delay constraints).

The impact is **governance authorization manipulation**: a minority token holder can transiently acquire majority or supermajority voting power on a specific proposal, causing it to pass or fail contrary to the true long-term token holder distribution. For SNS DAOs controlling treasury funds, canister upgrades, or parameter changes, this is a direct governance takeover vector on a per-proposal basis.

---

### Likelihood Explanation

- The attack requires no privileged access, no key compromise, and no subnet-majority corruption.
- The entry path is a standard unprivileged ingress call sequence: `claim_or_refresh_neuron` followed by `make_proposal` (or `increase_dissolve_delay` followed by `make_proposal`).
- The attacker must hold or transiently acquire SNS tokens, which is possible on any SNS with liquid token markets.
- The IC's sequential message processing within a subnet round makes the ordering deterministic and exploitable.
- Likelihood is **medium**: it requires capital and timing, but no technical barriers beyond standard canister interaction.

---

### Recommendation

Introduce a periodic voting power snapshot task for SNS governance, analogous to the NNS's `SnapshotVotingPowerTask` in `rs/nns/governance/src/timer_tasks/snapshot_voting_power.rs`. When `compute_ballots_for_new_proposal` is called, compare the current total potential voting power against the historical snapshot baseline. If a spike is detected (current > 1.5× historical minimum), use the historical snapshot's per-neuron voting power for ballot creation instead of live state. This is the exact mitigation already deployed for NNS governance.

---

### Proof of Concept

**Step 1**: Attacker holds neuron `N` with 100 SNS tokens staked (baseline voting power = 100 units).

**Step 2**: Attacker acquires 900 additional SNS tokens (e.g., from a secondary market or coordinated transfer).

**Step 3**: Attacker calls `claim_or_refresh_neuron` on neuron `N`, updating `cached_neuron_stake_e8s` to 1000 tokens.

**Step 4**: In the same ingress batch (or immediately after), attacker calls `make_proposal` to submit a proposal.

**Step 5**: `compute_ballots_for_new_proposal` executes at `rs/sns/governance/src/governance.rs:5226`, reads live `cached_neuron_stake_e8s = 1000`, and assigns ballot `voting_power = 1000 units` to neuron `N`.

**Step 6**: Attacker votes YES. With 10× inflated voting power, the proposal passes even if all other neurons vote NO (assuming attacker's inflated share exceeds the adoption threshold).

**Step 7**: After the voting period, attacker begins dissolving neuron `N` and recovers the 900 tokens.

The root cause is confirmed at: [1](#0-0) 

compared to the NNS mitigation at: [2](#0-1) 

with the periodic snapshot task that feeds it: [3](#0-2) 

and the snapshot collection structure: [4](#0-3)

### Citations

**File:** rs/sns/governance/src/governance.rs (L5255-5279)
```rust
        for (k, v) in self.proto.neurons.iter() {
            // If this neuron is eligible to vote, record its
            // voting power at the time of proposal creation (now).
            if v.dissolve_delay_seconds(now_seconds) < min_dissolve_delay_for_vote {
                // Not eligible due to dissolve delay.
                continue;
            }

            let voting_power = v.voting_power(
                now_seconds,
                max_dissolve_delay,
                max_age_bonus,
                max_dissolve_delay_bonus_percentage,
                max_age_bonus_percentage,
            );

            total_power += voting_power as u128;
            electoral_roll.insert(
                k.clone(),
                Ballot {
                    vote: Vote::Unspecified as i32,
                    voting_power,
                    cast_timestamp_seconds: 0,
                },
            );
```

**File:** rs/nns/governance/src/governance.rs (L5506-5512)
```rust
        let maybe_previous_ballots_if_voting_power_spike_detected = VOTING_POWER_SNAPSHOTS
            .with_borrow(|snapshots| {
                snapshots.previous_ballots_if_voting_power_spike_detected(
                    current_voting_power_snapshot.total_potential_voting_power(),
                    now_seconds,
                )
            });
```

**File:** rs/nns/governance/src/timer_tasks/snapshot_voting_power.rs (L30-57)
```rust
impl RecurringSyncTask for SnapshotVotingPowerTask {
    fn execute(self) -> (Duration, Self) {
        let now_seconds = self
            .governance
            .with_borrow(|governance| governance.env.now());
        if self
            .snapshots
            .with_borrow(|snapshots| snapshots.is_latest_snapshot_a_spike(now_seconds))
        {
            return (VOTING_POWER_SNAPSHOT_INTERVAL, self);
        }

        let voting_power_snapshot = self.governance.with_borrow_mut(|governance| {
            let voting_power_economics = governance.voting_power_economics();
            governance
                .neuron_store
                .compute_voting_power_snapshot_for_standard_proposal(
                    voting_power_economics,
                    now_seconds,
                )
                .expect("Voting power snapshot failed")
        });

        self.snapshots.with_borrow_mut(|snapshots| {
            snapshots.record_voting_power_snapshot(now_seconds, voting_power_snapshot);
        });

        (VOTING_POWER_SNAPSHOT_INTERVAL, self)
```

**File:** rs/nns/governance/src/governance/voting_power_snapshots.rs (L35-39)
```rust
pub(crate) struct VotingPowerSnapshots {
    neuron_id_to_voting_power_maps:
        StableBTreeMap<TimestampSeconds, NeuronIdToVotingPowerMap, DefaultMemory>,
    voting_power_totals: StableBTreeMap<TimestampSeconds, VotingPowerTotal, DefaultMemory>,
}
```
