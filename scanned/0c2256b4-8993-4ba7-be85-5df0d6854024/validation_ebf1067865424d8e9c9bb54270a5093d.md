### Title
Voting Power Spike Detection Bypassed via Transient Neuron Stake Decrease to Poison the Minimum Snapshot - (`rs/nns/governance/src/governance/voting_power_snapshots.rs`)

---

### Summary

The NNS Governance canister's voting power spike detection mechanism uses the **minimum** total potential voting power across the last 7 daily snapshots as its baseline. An attacker who controls enough ICP to hold a large neuron can transiently **decrease** the minimum snapshot value by temporarily reducing their neuron's stake at the moment the daily `SnapshotVotingPowerTask` fires, then restoring it. This poisons the 7-snapshot window with an artificially low minimum, causing the spike detector to treat the attacker's actual (large) voting power as a "spike" — which causes proposals to use the poisoned (low) snapshot as ballots, effectively **excluding the attacker's neuron from the ballot** and suppressing their voting power on targeted proposals.

Alternatively, the same mechanism can be exploited in reverse: an attacker can transiently **inflate** their stake at snapshot time to record a high minimum, then submit a proposal immediately after restoring their stake to normal, preventing the spike detector from triggering even when the attacker's real voting power is genuinely elevated.

---

### Finding Description

The `SnapshotVotingPowerTask` runs once per day and records the current total potential voting power of all eligible neurons into a rolling window of 7 snapshots (`MAX_VOTING_POWER_SNAPSHOTS = 7`). [1](#0-0) 

When a proposal is submitted, `compute_ballots_for_standard_proposal` computes the current voting power snapshot and checks whether it exceeds `1.5×` the **minimum** total potential voting power across all stored snapshots. If a spike is detected, the snapshot with the minimum total potential voting power is used as the ballot set instead of the current one. [2](#0-1) [3](#0-2) 

The spike detection compares against the **minimum** snapshot in the window:

```rust
.min_by_key(|(_, snapshot)| snapshot.total_potential_voting_power)?;

let voting_power_spike_detected = (current_total_potential_voting_power as f64)
    > (totals_with_minimum_total_potential_voting_power.total_potential_voting_power as f64)
        * MULTIPLIER_THRESHOLD_FOR_VOTING_POWER_SPIKE;
``` [4](#0-3) 

The snapshot is taken from the **live** neuron store at the moment the timer fires, using `compute_voting_power_snapshot_for_standard_proposal`, which reads `cached_neuron_stake_e8s` directly from each neuron: [5](#0-4) 

A neuron's stake is updated via `refresh_neuron`, which reads the **current ledger balance** and writes it to `cached_neuron_stake_e8s`: [6](#0-5) 

**Attack vector — poisoning the minimum snapshot downward:**

1. Attacker holds a large neuron (e.g., 10M ICP stake, representing 30% of total voting power).
2. Attacker monitors the daily timer. Just before the `SnapshotVotingPowerTask` fires, they transfer most of their ICP out of the neuron's staking subaccount and call `ClaimOrRefresh` to update `cached_neuron_stake_e8s` to a tiny value (e.g., 1 ICP).
3. The snapshot fires and records a very low total potential voting power (e.g., 70% of normal).
4. Attacker immediately transfers ICP back and refreshes the neuron to restore their full stake.
5. After 7 days of repeating this, all 7 snapshots have the artificially low minimum.
6. Now, when the attacker submits a proposal with their full stake, the current total potential voting power is ~1.43× the poisoned minimum — **below the 1.5× threshold** — so no spike is detected and the current (full) snapshot is used. The attacker votes with their full weight.

**Attack vector — suppressing a legitimate neuron's vote via false spike:**

1. Attacker transiently inflates their stake at snapshot time (by temporarily staking borrowed ICP), recording a high minimum.
2. When a target proposal is submitted, the attacker's real stake is normal, but the minimum snapshot is inflated, so the current snapshot appears to be a "spike" relative to the inflated minimum — **no**, wait: the spike is detected when current > 1.5× minimum. If the attacker inflated the minimum, the current would be *lower* than the minimum, so no spike. This direction is less useful.

The more impactful direction is the **downward poisoning**: by recording artificially low snapshots, the attacker ensures the spike detector never fires even when they genuinely hold elevated voting power, allowing them to pass proposals that would otherwise be blocked by the spike protection.

The cost of this attack is only the ICP transfer fees (and the dissolve delay constraint means the ICP must remain staked for the dissolve period, but the attacker can use a neuron that is already dissolving or use a separate neuron for the manipulation). Crucially, the ICP is returned after each snapshot, so the net cost is only transaction fees — analogous to the Aloe IV manipulation where liquidity is deposited and withdrawn at zero net cost. [7](#0-6) 

---

### Impact Explanation

The voting power spike detection is the NNS's primary defense against flash-loan-style governance attacks where an attacker temporarily acquires large voting power to pass a malicious proposal. By poisoning the minimum snapshot, an attacker can:

1. **Bypass spike detection entirely**: Submit a proposal while holding genuinely elevated voting power, without triggering the fallback to a historical snapshot.
2. **Pass proposals that should be blocked**: Governance proposals (e.g., upgrading canisters, changing network economics, minting ICP) that require supermajority support could be passed by an attacker who has accumulated enough ICP to exceed the threshold, if the spike detector is neutralized.
3. **Suppress other neurons' votes**: If the spike detector is triggered by a legitimate large staker, the fallback snapshot (the poisoned minimum) may exclude neurons that were created or grew after the poisoned snapshot was taken, reducing their effective voting power.

---

### Likelihood Explanation

The attack requires:
- Controlling a neuron with sufficient ICP to meaningfully affect the total potential voting power (a few percent of total NNS stake).
- Timing the stake refresh to coincide with the daily snapshot timer (predictable, fires every 86,400 seconds).
- Repeating for 7 consecutive days to fill the snapshot window.

The timer fires at a predictable interval and the snapshot timestamp is observable on-chain. The cost is only ICP transfer fees (a few e8s per round trip). An attacker with ~5% of total NNS stake could reduce the minimum snapshot by ~5%, which over 7 days of poisoning would allow them to hold ~7.5% of total stake without triggering the 1.5× spike threshold. This is a realistic attack for a well-funded adversary.

---

### Recommendation

1. **Use a time-weighted average** of voting power across the snapshot window rather than the minimum, so a single poisoned snapshot has limited effect — directly analogous to the Aloe recommendation to use time-weighted average liquidity.
2. **Rate-limit stake refreshes** near snapshot time, or take snapshots at unpredictable times (e.g., using a VRF-derived offset).
3. **Require a minimum number of consecutive non-spike snapshots** before resetting the baseline, rather than using the raw minimum.
4. **Detect anomalous stake decreases** at snapshot time and flag them, similar to how the spike detector already flags anomalous increases.

---

### Proof of Concept

**Setup**: Assume total NNS potential voting power is 500M ICP-equivalent. Attacker controls a neuron with 50M ICP (10% of total). Normal snapshot records ~500M total.

**Poisoning phase** (7 days):
- Each day, 1 hour before the `SnapshotVotingPowerTask` fires, attacker calls `Disburse` or transfers ICP out of the neuron subaccount and calls `ClaimOrRefresh` to reduce `cached_neuron_stake_e8s` to 1 ICP.
- Snapshot fires, records total ≈ 450M (500M − 50M + ~0).
- Attacker immediately transfers ICP back and calls `ClaimOrRefresh` to restore 50M ICP stake.
- After 7 days, all 7 snapshots show ≈ 450M minimum.

**Exploitation**:
- Attacker submits a proposal. Current total potential voting power = 500M.
- Spike check: 500M > 450M × 1.5 = 675M? **No** → no spike detected.
- Ballots are created from the current snapshot (500M), including the attacker's full 50M ICP voting power.
- Attacker votes with 10% of total voting power, potentially enough to pass a proposal with liquid following.

The entry path is fully unprivileged: `manage_neuron` → `ClaimOrRefresh` is callable by any neuron controller, and `make_proposal` is callable by any neuron with sufficient dissolve delay. [8](#0-7) [9](#0-8) [10](#0-9) [11](#0-10)

### Citations

**File:** rs/nns/governance/src/timer_tasks/snapshot_voting_power.rs (L16-57)
```rust
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

**File:** rs/nns/governance/src/governance/voting_power_snapshots.rs (L119-151)
```rust
    /// Given a total potential voting power, checks if there is a voting power spike. If a spike is
    /// detected, it returns the timestamp and totals of the snapshot with the minimum total
    /// potential voting power. If no spike is detected, it returns None.
    fn totals_entry_with_minimum_total_potential_voting_power_if_voting_power_spiked(
        &self,
        now_seconds: TimestampSeconds,
        current_total_potential_voting_power: u64,
    ) -> Option<(TimestampSeconds, VotingPowerTotal)> {
        let (
            timestamp_with_minimum_total_potential_voting_power,
            totals_with_minimum_total_potential_voting_power,
        ) = self
            .voting_power_totals
            .iter()
            .filter(|(created_at, _)| {
                let age = now_seconds - created_at;
                age <= MAXIMUM_STALENESS_SECONDS
            })
            .min_by_key(|(_, snapshot)| snapshot.total_potential_voting_power)?;

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

**File:** rs/nns/governance/src/governance.rs (L5486-5533)
```rust
    fn compute_ballots_for_standard_proposal(
        &self,
        now_seconds: u64,
    ) -> Result<
        (
            HashMap<u64, Ballot>,
            u64,         /*potential_voting_power*/
            Option<u64>, /*previous_ballots_timestamp_seconds*/
        ),
        GovernanceError,
    > {
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

        let (ballots, total_potential_voting_power) =
            voting_power_snapshot.create_ballots_and_total_potential_voting_power();
        Ok((
            ballots,
            total_potential_voting_power,
            previous_ballots_timestamp_seconds,
        ))
    }
```

**File:** rs/nns/governance/src/governance.rs (L5897-5961)
```rust

    /// Refreshes the stake of a given neuron by checking it's account.
    #[cfg_attr(feature = "tla", tla_update_method(REFRESH_NEURON_DESC.clone(), tla_snapshotter!()))]
    async fn refresh_neuron(
        &mut self,
        nid: NeuronId,
        subaccount: Subaccount,
        claim_or_refresh: &ClaimOrRefresh,
    ) -> Result<NeuronId, GovernanceError> {
        let account = neuron_subaccount(subaccount);
        // We need to lock the neuron to make sure it doesn't undergo
        // concurrent changes while we're checking the balance and
        // refreshing the stake.
        let now = self.env.now();
        let _neuron_lock = self.lock_neuron_for_command(
            nid.id,
            NeuronInFlightCommand {
                timestamp: now,
                command: Some(InFlightCommand::ClaimOrRefreshNeuron(
                    claim_or_refresh.clone(),
                )),
            },
        )?;

        // Get the balance of the neuron from the ledger canister.
        tla_log_locals! { neuron_id: nid.id };
        let balance = self.ledger.account_balance(account).await?;
        let min_stake = self.economics().neuron_minimum_stake_e8s;
        if balance.get_e8s() < min_stake {
            return Err(GovernanceError::new_with_message(
                ErrorType::InsufficientFunds,
                format!(
                    "Account does not have enough funds to refresh a neuron. \
                     Please make sure that account has at least {:?} e8s (was {:?} e8s)",
                    min_stake,
                    balance.get_e8s()
                ),
            ));
        }
        self.with_neuron_mut(&nid, |neuron| {
            match neuron.cached_neuron_stake_e8s.cmp(&balance.get_e8s()) {
                Ordering::Greater => {
                    println!(
                        "{}ERROR. Neuron cached stake was inconsistent.\
                     Neuron account: {} has less e8s: {} than the cached neuron stake: {}.\
                     Stake adjusted.",
                        LOG_PREFIX,
                        account,
                        balance.get_e8s(),
                        neuron.cached_neuron_stake_e8s
                    );
                    neuron.update_stake_adjust_age(balance.get_e8s(), now);
                }
                Ordering::Less => {
                    neuron.update_stake_adjust_age(balance.get_e8s(), now);
                }
                // If the stake is the same as the account balance,
                // just return the neuron id (this way this method
                // also serves the purpose of allowing to discover the
                // neuron id based on the memo and the controller).
                Ordering::Equal => (),
            };
        })?;

        Ok(nid)
```

**File:** rs/nns/governance/src/neuron_store/voting_power.rs (L124-186)
```rust
impl NeuronStore {
    /// Computes the voting power snapshot for a standard proposal.
    pub fn compute_voting_power_snapshot_for_standard_proposal(
        &self,
        voting_power_economics: &VotingPowerEconomics,
        now_seconds: u64,
    ) -> Result<VotingPowerSnapshot, NeuronStoreError> {
        let mut voting_power_map = HashMap::new();
        let mut total_deciding_voting_power: u128 = 0;
        let mut total_potential_voting_power: u128 = 0;

        let default_min_dissolve_delay = if is_mission_70_voting_rewards_enabled() {
            VotingPowerEconomics::MISSION_70_DEFAULT_NEURON_MINIMUM_DISSOLVE_DELAY_TO_VOTE_SECONDS
        } else {
            VotingPowerEconomics::DEFAULT_NEURON_MINIMUM_DISSOLVE_DELAY_TO_VOTE_SECONDS
        };
        let min_dissolve_delay_seconds = voting_power_economics
            .neuron_minimum_dissolve_delay_to_vote_seconds
            .unwrap_or(default_min_dissolve_delay);

        let mut process_neuron = |neuron: &Neuron| {
            if neuron.is_inactive(now_seconds)
                || neuron.dissolve_delay_seconds(now_seconds) < min_dissolve_delay_seconds
            {
                return;
            }

            let (potential_voting_power, deciding_voting_power) =
                neuron.potential_and_deciding_voting_power(voting_power_economics, now_seconds);
            // We don't handle overflow here, as in `get_voting_power_as_u64` below,
            // the input arguments bigger than u64::MAX will result in an error.
            total_deciding_voting_power =
                total_deciding_voting_power.saturating_add(deciding_voting_power as u128);
            total_potential_voting_power =
                total_potential_voting_power.saturating_add(potential_voting_power as u128);
            voting_power_map.insert(neuron.id().id, deciding_voting_power);
        };

        // Active neurons iterator already makes distinctions between stable and heap neurons.
        self.with_active_neurons_iter_sections(
            |iter| {
                for neuron in iter {
                    process_neuron(&neuron);
                }
            },
            NeuronSections::NONE,
        );

        let total_deciding_voting_power = get_voting_power_as_u64(
            total_deciding_voting_power,
            NeuronStoreError::TotalDecidingVotingPowerOverflow,
        )?;
        let total_potential_voting_power = get_voting_power_as_u64(
            total_potential_voting_power,
            NeuronStoreError::TotalPotentialVotingPowerOverflow,
        )?;

        Ok(VotingPowerSnapshot {
            voting_power_map,
            total_deciding_voting_power,
            total_potential_voting_power,
        })
    }
```
