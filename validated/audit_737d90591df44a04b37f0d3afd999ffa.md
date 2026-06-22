### Title
SNS Governance Lacks Voting Power Spike Detection, Enabling Temporary Stake Inflation to Manipulate Proposal Outcomes - (`File: rs/sns/governance/src/governance.rs`)

### Summary

The SNS governance canister computes ballot voting power at the exact moment of proposal creation with no historical comparison or spike detection. An attacker who temporarily acquires a large quantity of SNS tokens, stakes them into a neuron, and creates a proposal can pass that proposal using artificially inflated voting power. The NNS governance canister has an explicit mitigation for this class of attack (`VotingPowerSnapshots` + spike detection), but the SNS governance canister has no equivalent protection.

### Finding Description

In `rs/sns/governance/src/governance.rs`, `compute_ballots_for_new_proposal` iterates over all neurons and records each neuron's voting power at `now_seconds` — the current canister time — with no reference to any historical baseline: [1](#0-0) 

The voting power of each neuron is computed as `stake * dissolve_delay_bonus * age_bonus * multiplier`: [2](#0-1) 

There is no mechanism in the SNS governance codebase to detect whether the current total voting power is anomalously high compared to recent history. A grep for `VotingPowerSnapshots`, `MULTIPLIER_THRESHOLD`, `previous_ballots`, or `voting_power_spike` in `rs/sns/**` returns zero results.

By contrast, the NNS governance canister has an explicit mitigation. `compute_ballots_for_standard_proposal` computes the current snapshot and then checks it against up to 7 daily historical snapshots stored in `VotingPowerSnapshots`: [3](#0-2) 

If the current total potential voting power exceeds 1.5× the minimum of the stored snapshots, the NNS falls back to the historical snapshot with the minimum total voting power: [4](#0-3) 

The NNS CHANGELOG explicitly records this as a security fix deployed in Proposal 137252: [5](#0-4) 

The SNS governance canister has no analogous `VotingPowerSnapshots` structure, no `SnapshotVotingPowerTask`, and no spike-detection call in `compute_ballots_for_new_proposal`.

### Impact Explanation

An attacker can:

1. Acquire a large quantity of SNS tokens (e.g., via a DEX or OTC).
2. Stake them into a new neuron with a dissolve delay ≥ `neuron_minimum_dissolve_delay_to_vote_seconds`.
3. Call `make_proposal` — ballots are created using the inflated voting power at that instant.
4. Vote `Yes` on the proposal with the newly created neuron.
5. If the attacker's stake is large enough relative to the existing token distribution, the proposal passes immediately (absolute majority) or within the voting period.
6. Begin dissolving the neuron to recover the tokens.

Proposals that can be passed include `TransferSnsTreasuryFunds` (drain the SNS treasury), `UpgradeSnsControlledCanister` (install malicious code in dapp canisters), `ManageNervousSystemParameters` (lower quorum thresholds for future attacks), and `ExecuteGenericNervousSystemFunction` (call arbitrary registered functions). [6](#0-5) 

### Likelihood Explanation

The attack requires capital proportional to the existing SNS token distribution. For SNS DAOs with low total staked voting power or thin token markets, the cost is low. The attack is fully deterministic and requires no privileged access — only the ability to call `manage_neuron` (stake) and `make_proposal` as an ordinary principal. The dissolve delay requirement locks capital for a period but does not prevent the attack; the attacker recovers tokens after the delay. The NNS team judged this class of attack realistic enough to deploy a dedicated mitigation (Proposal 137252).

### Recommendation

Implement a `VotingPowerSnapshots` mechanism for SNS governance analogous to the one in NNS governance:

- Add a recurring timer task that snapshots total potential voting power daily.
- In `compute_ballots_for_new_proposal`, compare the current total voting power against the minimum of the last N snapshots.
- If the current total exceeds `MULTIPLIER_THRESHOLD_FOR_VOTING_POWER_SPIKE` (1.5×) of the minimum, use the historical snapshot with the minimum total voting power to construct ballots instead of the current inflated state. [7](#0-6) [8](#0-7) 

### Proof of Concept

**Setup**: An SNS with 10 existing neurons each holding 100,000 SNS tokens staked (total staked: 1,000,000 tokens). The attacker acquires 2,000,001 SNS tokens.

**Steps**:

1. Attacker calls `manage_neuron { command: Stake }` to create a neuron with 2,000,001 tokens and a dissolve delay of `neuron_minimum_dissolve_delay_to_vote_seconds`.
2. Attacker calls `make_proposal` with action `TransferSnsTreasuryFunds { amount: <entire treasury> }`.
3. `compute_ballots_for_new_proposal` runs at `now_seconds`:
   - Existing neurons: total voting power ≈ 1,000,000 (no dissolve delay bonus assumed for simplicity).
   - Attacker neuron: voting power ≈ 2,000,001 (with minimal dissolve delay bonus).
   - Total: ≈ 3,000,001. Attacker share: > 66%.
4. The proposer's neuron automatically votes `Yes` on proposal creation.
5. Attacker's voting power (2,000,001) > 50% of total (3,000,001) → proposal is decided immediately as `Approved`.
6. Treasury is drained. Attacker starts dissolving the neuron.

No spike detection fires because `compute_ballots_for_new_proposal` in SNS has no such check: [9](#0-8)

### Citations

**File:** rs/sns/governance/src/governance.rs (L5225-5227)
```rust
    /// Computes the total potential voting power of the governance canister and ballots.
    fn compute_ballots_for_new_proposal(&self) -> Result<(u64, BTreeMap<String, Ballot>), String> {
        let now_seconds = self.env.now();
```

**File:** rs/sns/governance/src/governance.rs (L5255-5280)
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
        }
```

**File:** rs/sns/governance/src/neuron.rs (L196-251)
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
```

**File:** rs/nns/governance/src/governance.rs (L5497-5524)
```rust
        let current_voting_power_snapshot = self
            .neuron_store
            .compute_voting_power_snapshot_for_standard_proposal(
                self.voting_power_economics(),
                now_seconds,
            )?;

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

**File:** rs/nns/governance/CHANGELOG.md (L453-461)
```markdown
# 2025-07-06: Proposal 137252

http://dashboard.internetcomputer.org/proposal/137252

## Added

* Add a metric for the nubmer of spawning neurons.
* Use a previous voting power snapshot to create ballots if a voting power spike is detected.

```

**File:** rs/nns/governance/src/timer_tasks/snapshot_voting_power.rs (L9-57)
```rust
/// A task to snapshot the voting power every day, so that the snapshot can be used to disable
/// early adoption of proposals if such proposals have unusually high voting power.
pub(super) struct SnapshotVotingPowerTask {
    governance: &'static LocalKey<RefCell<Governance>>,
    snapshots: &'static LocalKey<RefCell<VotingPowerSnapshots>>,
}

const VOTING_POWER_SNAPSHOT_INTERVAL: Duration = Duration::from_secs(60 * 60 * 24);

impl SnapshotVotingPowerTask {
    pub fn new(
        governance: &'static LocalKey<RefCell<Governance>>,
        snapshots: &'static LocalKey<RefCell<VotingPowerSnapshots>>,
    ) -> Self {
        Self {
            governance,
            snapshots,
        }
    }
}

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
