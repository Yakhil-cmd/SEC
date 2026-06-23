### Title
Missing Voting Power Snapshot Mechanism in SNS Governance Allows Immediate Governance Takeover - (File: rs/sns/governance/src/governance.rs)

### Summary
The SNS governance canister lacks the voting power snapshot mechanism present in NNS governance. When a proposal is created, ballots are assigned based on the current live voting power of all neurons with no historical baseline comparison. An unprivileged SNS token holder who suddenly acquires a large stake can immediately submit a proposal and exercise their full inflated voting power, potentially passing it via early adoption before other participants can react.

### Finding Description

The NNS governance canister implements a `VotingPowerSnapshots` mechanism that takes daily snapshots of total potential voting power and detects "spikes." When a spike is detected (current total > 1.5× the minimum in the last 7 daily snapshots), proposal ballots are created from the historical snapshot rather than the current live state, preventing sudden large stakers from immediately dominating governance. [1](#0-0) [2](#0-1) 

The SNS governance canister has no equivalent mechanism. Its `compute_ballots_for_new_proposal` function iterates all current neurons and assigns voting power purely from live state at proposal creation time: [3](#0-2) 

There is no spike detection, no historical snapshot store, and no fallback to a previous baseline. The function simply reads `self.proto.neurons.iter()` and calls `v.voting_power(now_seconds, ...)` for each neuron, inserting the result directly into the ballot. [4](#0-3) 

The SNS neuron voting power is computed from stake, dissolve delay bonus, age bonus, and a `voting_power_percentage_multiplier`, all of which are live values: [5](#0-4) 

### Impact Explanation

An attacker who acquires a large block of SNS tokens and stakes them in a neuron with sufficient dissolve delay can immediately submit a proposal. If their voting power exceeds 50% of the total, the proposal passes via early adoption in the same block. Eligible proposal actions include `TransferSnsTreasuryFunds` (draining the SNS treasury), `UpgradeSnsControlledCanister` (replacing dapp code with malicious code), and `ManageNervousSystemParameters` (altering governance rules). This constitutes a complete governance takeover with no time window for defenders to respond. [6](#0-5) 

### Likelihood Explanation

The attack is economically constrained — the attacker must acquire >50% of the circulating voting power (staked tokens × dissolve delay bonus). For SNS DAOs with low staking participation or concentrated token distribution (common in early-stage SNS projects), this threshold may be reachable via DEX purchases or flash-loan-style coordination. The attack requires no privileged access, no admin keys, and no social engineering — only a sufficiently large token acquisition followed by a standard `manage_neuron` stake and `make_proposal` ingress call. The NNS itself recognized this exact risk and deployed the snapshot mechanism to address it; the SNS has not received the same protection.

### Recommendation

Implement a voting power snapshot mechanism in the SNS governance canister analogous to the NNS `VotingPowerSnapshots` in `rs/nns/governance/src/governance/voting_power_snapshots.rs`. Specifically:

1. Add a recurring timer task (analogous to `SnapshotVotingPowerTask`) that records total voting power periodically.
2. In `compute_ballots_for_new_proposal`, compare the current total voting power against the historical minimum. If a spike is detected (e.g., current > 1.5× minimum), use the previous snapshot's voting power distribution for ballot creation.
3. Define a staleness bound so that very old snapshots are not used. [7](#0-6) 

### Proof of Concept

1. Observe that SNS governance `compute_ballots_for_new_proposal` has no snapshot comparison:
   `rs/sns/governance/src/governance.rs` lines 5225–5294 — no reference to any snapshot store, no spike detection, no historical baseline.

2. Contrast with NNS governance `compute_ballots_for_standard_proposal`:
   `rs/nns/governance/src/governance.rs` lines 5497–5532 — explicitly calls `VOTING_POWER_SNAPSHOTS.with_borrow(|snapshots| snapshots.previous_ballots_if_voting_power_spike_detected(...))` before creating ballots.

3. Attack sequence (all via standard ingress):
   - Attacker calls SNS ledger `icrc1_transfer` to fund their account.
   - Attacker calls SNS governance `manage_neuron` → `ClaimOrRefresh` to stake a large neuron with dissolve delay ≥ `neuron_minimum_dissolve_delay_to_vote_seconds`.
   - Attacker immediately calls SNS governance `make_proposal` with action `TransferSnsTreasuryFunds`.
   - Ballots are created with the attacker's full current voting power. If it exceeds 50% of total, the proposal is adopted immediately via early adoption, executing the treasury drain in the same round. [8](#0-7)

### Citations

**File:** rs/nns/governance/src/governance/voting_power_snapshots.rs (L16-25)
```rust
/// The maximum number of voting power snapshots to keep.
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

**File:** rs/sns/governance/src/governance.rs (L3549-3559)
```rust
        // === Preparation
        //
        // Every neuron with a dissolve delay of at least
        // NervousSystemParameters.neuron_minimum_dissolve_delay_to_vote_seconds
        // is allowed to vote, with a voting power determined at the time of the
        // proposal creation (i.e., now).
        //
        // The electoral roll to put into the proposal.
        let (_, electoral_roll) = self
            .compute_ballots_for_new_proposal()
            .map_err(|err| GovernanceError::new_with_message(ErrorType::PreconditionFailed, err))?;
```

**File:** rs/sns/governance/src/governance.rs (L5225-5294)
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
```

**File:** rs/sns/governance/src/neuron.rs (L196-252)
```rust
    pub fn voting_power(
        &self,
        now_seconds: u64,
        max_dissolve_delay_seconds: u64,
        max_neuron_age_for_age_bonus: u64,
        max_dissolve_delay_bonus_percentage: u64,
        max_age_bonus_percentage: u64,
    ) -> u64 {
        // We compute the stake adjustments in u128.
        let stake = self.voting_power_stake_e8s() as u128;
        // Dissolve delay is capped to max_dissolve_delay_seconds, but we cap it
        // again here to make sure, e.g., if this changes in the future.
        let d = std::cmp::min(
            self.dissolve_delay_seconds(now_seconds),
            max_dissolve_delay_seconds,
        ) as u128;
        // 'd_stake' is the stake with bonus for dissolve delay.
        let d_stake = stake
            + if max_dissolve_delay_seconds > 0 {
                (stake * d * max_dissolve_delay_bonus_percentage as u128)
                    / (100 * max_dissolve_delay_seconds as u128)
            } else {
                0
            };
        // Sanity check.
        assert!(d_stake <= stake + (stake * (max_dissolve_delay_bonus_percentage as u128) / 100));
        // The voting power is also a function of the age of the
        // neuron, giving a bonus of up to max_age_bonus_percentage at max_neuron_age_for_age_bonus.
        let a = std::cmp::min(self.age_seconds(now_seconds), max_neuron_age_for_age_bonus) as u128;
        let ad_stake = d_stake
            + if max_neuron_age_for_age_bonus > 0 {
                (d_stake * a * max_age_bonus_percentage as u128)
                    / (100 * max_neuron_age_for_age_bonus as u128)
            } else {
                0
            };
        // Final stake 'ad_stake' has is not more than max_age_bonus_percentage above 'd_stake'.
        assert!(ad_stake <= d_stake + (d_stake * (max_age_bonus_percentage as u128) / 100));

        // Convert the multiplier to u128. The voting_power_percentage_multiplier represents
        // a percent and will always be within the range 0 to 100.
        let v = self.voting_power_percentage_multiplier as u128;

        // Apply the multiplier to 'ad_stake' and divide by 100 to have the same effect as
        // multiplying by a percent.
        let vad_stake = ad_stake
            .checked_mul(v)
            .expect("Overflow detected when calculating voting power")
            .checked_div(100)
            .expect("Underflow detected when calculating voting power");

        // The final voting power is the stake adjusted by both age,
        // dissolve delay, and voting power multiplier. If the stake is is greater than
        // u64::MAX divided by 2.5, the voting power may actually not
        // fit in a u64.
        std::cmp::min(vad_stake, u64::MAX as u128) as u64
    }
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
