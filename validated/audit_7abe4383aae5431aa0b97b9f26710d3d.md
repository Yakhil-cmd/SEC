### Title
SNS Governance Lacks Voting Power Spike Detection, Allowing Immediate Proposal Passage After Large Stake Acquisition - (`File: rs/sns/governance/src/governance.rs`)

### Summary

The SNS governance canister's `compute_ballots_for_new_proposal` function computes ballots using each neuron's current voting power at proposal creation time, with no mechanism to detect or mitigate a sudden spike in total voting power. The NNS governance canister has an explicit `VotingPowerSnapshots` system and spike-detection logic that prevents a newly large staker from immediately passing proposals via early adoption. The SNS governance canister has no equivalent protection, meaning an attacker who acquires a large SNS token stake can immediately submit a proposal, automatically vote YES as the proposer, and — if their stake exceeds the early-adoption threshold — pass any proposal (including treasury drains or malicious canister upgrades) in a single transaction sequence.

### Finding Description

In `rs/sns/governance/src/governance.rs`, `compute_ballots_for_new_proposal` iterates over all current neurons and records each neuron's voting power at `now_seconds`: [1](#0-0) 

There is no snapshot history, no comparison against prior total voting power, and no spike-detection gate. The function simply returns the current electoral roll.

By contrast, the NNS governance canister maintains a `VotingPowerSnapshots` store that records daily snapshots of total potential voting power: [2](#0-1) 

When a standard NNS proposal is created, `compute_ballots_for_standard_proposal` checks whether the current total potential voting power exceeds 1.5× the minimum of the last seven daily snapshots. If a spike is detected, it falls back to the snapshot with the minimum total voting power: [3](#0-2) 

The spike threshold constant and staleness window are: [4](#0-3) 

The daily snapshot task that feeds this mechanism: [5](#0-4) 

No analogous timer task, snapshot store, or spike-detection call exists anywhere in `rs/sns/governance/`. The glob search for `rs/sns/governance/src/governance/voting_power_snapshots*` and `rs/sns/governance/src/timer_tasks*` returns empty results, confirming the complete absence of this protection.

The SNS proposal creation flow calls `compute_ballots_for_new_proposal` directly: [6](#0-5) 

After ballots are created, the proposer's neuron automatically votes YES: [7](#0-6) 

The early-adoption rule means that if the proposer's ballot voting power exceeds 50% of `total` in the tally, the proposal is decided immediately: [8](#0-7) 

### Impact Explanation

An attacker who acquires enough SNS tokens to exceed the early-adoption threshold (>50% of total voting power, or the SNS-specific `minimum_yes_proportion_of_total`) can:

1. Stake tokens into a neuron with sufficient dissolve delay.
2. Immediately submit any proposal (e.g., `TransferSnsTreasuryFunds`, `UpgradeSnsControlledCanister`, `ExecuteGenericNervousSystemFunction`).
3. The proposer's neuron automatically votes YES.
4. Because the attacker's neuron dominates the ballot total, the proposal is adopted immediately without waiting for the voting period.

This allows a single actor to drain the SNS treasury, install malicious canister code, or execute arbitrary generic functions — all in a single block, before any other neuron holder can react.

The SNS ballot structure confirms voting power is fixed at proposal creation time and cannot be changed after the fact: [9](#0-8) 

### Likelihood Explanation

The attacker must acquire a dominant token position in the SNS, which is an economic barrier. However:

- SNS token swaps can result in concentrated initial distributions.
- Flash-loan-style attacks are not possible on the IC (no atomic composability across canisters in a single transaction), but a coordinated acquisition over a short period followed by immediate proposal submission is realistic.
- The attack is fully deterministic once the stake threshold is met — no randomness, no timing race, no privileged access required.
- The NNS governance team explicitly identified this as a real threat and deployed the spike-detection mechanism (Proposal 137252, July 2025) for NNS. The SNS canister did not receive the same fix.

The entry path is an unprivileged ingress call to `manage_neuron` (stake + claim neuron, then make proposal) — fully reachable by any ledger/governance user.

### Recommendation

Port the `VotingPowerSnapshots` mechanism from NNS governance to SNS governance:

1. Add a `StableBTreeMap`-backed snapshot store to SNS governance stable memory.
2. Add a recurring timer task (analogous to `SnapshotVotingPowerTask`) that records daily snapshots of total potential voting power.
3. In `compute_ballots_for_new_proposal`, compare the current total voting power against the snapshot history. If a spike is detected (e.g., current > 1.5× minimum of last N snapshots), use the snapshot with the minimum total voting power to construct ballots instead of the current state.
4. Tune the spike multiplier and snapshot window to match SNS governance's typical token distribution dynamics.

### Proof of Concept

**Setup:** An SNS has 1,000,000 SNS tokens staked across existing neurons.

**Attack:**
1. Attacker acquires 1,100,000 SNS tokens from the open market or swap.
2. Attacker calls `manage_neuron` → `ClaimOrRefresh` to stake into a neuron with dissolve delay ≥ `neuron_minimum_dissolve_delay_to_vote_seconds`.
3. Attacker calls `manage_neuron` → `MakeProposal` with action `TransferSnsTreasuryFunds` targeting the attacker's own account.
4. `compute_ballots_for_new_proposal` runs at `now_seconds`:
   - Attacker's neuron: voting power ≈ 1,100,000 (plus bonuses).
   - All other neurons: voting power ≈ 1,000,000 total.
   - Attacker's share: ~52% of total.
5. The proposer automatically votes YES with 52% of total voting power.
6. Early-adoption check: YES > 50% of total → proposal is immediately adopted and executed.
7. SNS treasury funds are transferred to the attacker.

The entire attack requires only two `manage_neuron` ingress calls. No governance majority, no privileged key, no social engineering. [10](#0-9) [11](#0-10)

### Citations

**File:** rs/sns/governance/src/governance.rs (L3557-3559)
```rust
        let (_, electoral_roll) = self
            .compute_ballots_for_new_proposal()
            .map_err(|err| GovernanceError::new_with_message(ErrorType::PreconditionFailed, err))?;
```

**File:** rs/sns/governance/src/governance.rs (L3606-3614)
```rust
        let mut proposal_data = ProposalData {
            action: u64::from(action),
            id: Some(proposal_id),
            proposer: Some(proposer_id.clone()),
            reject_cost_e8s,
            proposal: Some(proposal.clone()),
            proposal_creation_timestamp_seconds: now_seconds,
            ballots: electoral_roll,
            payload_text_rendering: Some(rendering),
```

**File:** rs/sns/governance/src/governance.rs (L5225-5295)
```rust
    /// Computes the total potential voting power of the governance canister and ballots.
    fn compute_ballots_for_new_proposal(&self) -> Result<(u64, BTreeMap<String, Ballot>), String> {
        let now_seconds = self.env.now();

        let nervous_system_parameters = self.nervous_system_parameters_or_panic();

        // Voting power bonus parameters.
        let max_dissolve_delay = nervous_system_parameters
            .max_dissolve_delay_seconds
            .expect("NervousSystemParameters must have max_dissolve_delay_seconds");

        let max_age_bonus = nervous_system_parameters
            .max_neuron_age_for_age_bonus
            .expect("NervousSystemParameters must have max_neuron_age_for_age_bonus");

        let max_dissolve_delay_bonus_percentage = nervous_system_parameters
            .max_dissolve_delay_bonus_percentage
            .expect("NervousSystemParameters must have max_dissolve_delay_bonus_percentage");

        let max_age_bonus_percentage = nervous_system_parameters
            .max_age_bonus_percentage
            .expect("NervousSystemParameters must have max_age_bonus_percentage");

        let min_dissolve_delay_for_vote = nervous_system_parameters
            .neuron_minimum_dissolve_delay_to_vote_seconds
            .expect("NervousSystemParameters must have min_dissolve_delay_for_vote");

        let mut electoral_roll = BTreeMap::<String, Ballot>::new();
        let mut total_power: u128 = 0;

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
        }

        if total_power >= (u64::MAX as u128) {
            // The way the neurons are configured, the total voting
            // power on this proposal would overflow a u64!
            return Err("Voting power overflow.".to_string());
        }
        if electoral_roll.is_empty() {
            // Cannot make a proposal with no eligible voters.  This
            // is a precaution that shouldn't happen as we check that
            // the voter is allowed to vote.
            return Err("No eligible voters.".to_string());
        }

        Ok((total_power as u64, electoral_roll))
    }
```

**File:** rs/nns/governance/src/governance/voting_power_snapshots.rs (L17-25)
```rust
const MAX_VOTING_POWER_SNAPSHOTS: u64 = 7;
/// The multiplier used to define what is a "voting power spike": if the current total voting
/// power is more than this multiplier times the minimum total voting power in the snapshots,
/// then we consider it a spike.
const MULTIPLIER_THRESHOLD_FOR_VOTING_POWER_SPIKE: f64 = 1.5;
/// The maximum staleness of a voting power snapshot. This is usually not needed since
/// the snapshots should be added frequently. However, we do not want to use a snapshot that is too
/// old, in the event of a failure in taking the snapshots.
const MAXIMUM_STALENESS_SECONDS: u64 = ONE_MONTH_SECONDS * 3;
```

**File:** rs/nns/governance/src/governance/voting_power_snapshots.rs (L35-39)
```rust
pub(crate) struct VotingPowerSnapshots {
    neuron_id_to_voting_power_maps:
        StableBTreeMap<TimestampSeconds, NeuronIdToVotingPowerMap, DefaultMemory>,
    voting_power_totals: StableBTreeMap<TimestampSeconds, VotingPowerTotal, DefaultMemory>,
}
```

**File:** rs/nns/governance/src/governance/voting_power_snapshots.rs (L139-151)
```rust
        let voting_power_spike_detected = (current_total_potential_voting_power as f64)
            > (totals_with_minimum_total_potential_voting_power.total_potential_voting_power
                as f64)
                * MULTIPLIER_THRESHOLD_FOR_VOTING_POWER_SPIKE;
        if voting_power_spike_detected {
            Some((
                timestamp_with_minimum_total_potential_voting_power,
                totals_with_minimum_total_potential_voting_power,
            ))
        } else {
            None
        }
    }
```

**File:** rs/nns/governance/src/governance.rs (L5504-5524)
```rust
        // Check if there is a voting power spike. If there is, then the return value here
        // will be `Some(...)`.
        let maybe_previous_ballots_if_voting_power_spike_detected = VOTING_POWER_SNAPSHOTS
            .with_borrow(|snapshots| {
                snapshots.previous_ballots_if_voting_power_spike_detected(
                    current_voting_power_snapshot.total_potential_voting_power(),
                    now_seconds,
                )
            });

        let (voting_power_snapshot, previous_ballots_timestamp_seconds) =
            match maybe_previous_ballots_if_voting_power_spike_detected {
                // This is the extraordinary case - we have a voting power spike, and we
                // need to use the previous snapshot.
                Some((previous_snapshot_timestamp, previous_snapshot)) => {
                    (previous_snapshot, Some(previous_snapshot_timestamp))
                }
                // This is the normal case - we have no voting power spike, so we use the
                // current snapshot.
                None => (current_voting_power_snapshot, None),
            };
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

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L1265-1269)
```rust
    /// The voting power associated with the ballot. The voting power of a ballot
    /// associated with a neuron and a proposal is set at the proposal's creation
    /// time to the neuron's voting power at that time.
    #[prost(uint64, tag = "2")]
    pub voting_power: u64,
```

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L1294-1310)
```rust
pub struct Tally {
    /// The time when this tally was made, in seconds from the Unix epoch.
    #[prost(uint64, tag = "1")]
    pub timestamp_seconds: u64,
    /// The number of yes votes, in voting power unit.
    #[prost(uint64, tag = "2")]
    pub yes: u64,
    /// The number of no votes, in voting power unit.
    #[prost(uint64, tag = "3")]
    pub no: u64,
    /// The total voting power unit of eligible neurons that can vote
    /// on the proposal that this tally is associated with (i.e., the sum
    /// of the voting power of yes, no, and undecided votes).
    /// This should always be greater than or equal to yes + no.
    #[prost(uint64, tag = "4")]
    pub total: u64,
}
```
