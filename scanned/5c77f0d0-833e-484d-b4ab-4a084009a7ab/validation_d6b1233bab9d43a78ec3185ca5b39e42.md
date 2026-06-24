### Title
SNS Governance Reward Dilution via Stake-Before-Proposal Timing Attack — (`rs/sns/governance/src/governance.rs`)

### Summary

SNS governance computes proposal ballots by iterating over all neurons at proposal-creation time with no voting-power spike detection. Unlike NNS governance, which explicitly guards against sudden large stake entries via `VotingPowerSnapshots`, SNS governance has no such protection. An unprivileged actor can stake a large amount of SNS tokens immediately before a proposal is created, vote (via following), capture a disproportionate share of the reward pool, and exit after the minimum dissolve delay — directly diluting rewards for all pre-existing stakers.

---

### Finding Description

**NNS has explicit spike detection; SNS does not.**

In NNS governance, `compute_ballots_for_standard_proposal` checks `VotingPowerSnapshots` before creating ballots. If the current total potential voting power exceeds 1.5× the minimum recorded in recent snapshots, the system falls back to a previous snapshot, preventing a sudden large stake from inflating the attacker's ballot share. [1](#0-0) [2](#0-1) 

In SNS governance, `compute_ballots_for_new_proposal` simply iterates over `self.proto.neurons` at the moment of proposal creation, computing each neuron's voting power from its current stake and dissolve delay. There is no snapshot history, no spike threshold, and no fallback mechanism. [3](#0-2) 

Reward shares are then allocated proportionally to `ballot.voting_power`: [4](#0-3) 

The total reward pool is `supply × reward_rate × round_duration`, where `supply` is fetched live from the ledger just before distribution: [5](#0-4) [6](#0-5) 

An attacker who stakes a large amount just before proposal creation inflates `total_reward_shares`, reducing every other voter's fraction `neuron_reward_shares / total_reward_shares`. [7](#0-6) 

After collecting maturity, the attacker disburses it (7-day delay via `MATURITY_DISBURSEMENT_DELAY_SECONDS`) and dissolves the neuron after the minimum dissolve delay: [8](#0-7) 

---

### Impact Explanation

Every existing SNS staker who voted on the targeted proposal receives a smaller maturity reward than they would have without the attack. The attacker captures a portion of the reward pool proportional to their injected stake, net of the opportunity cost of locking tokens for the minimum dissolve delay. For SNS instances with short `neuron_minimum_dissolve_delay_to_vote_seconds` (configurable down to 0) and high initial reward rates, the attack is economically profitable. The reward pool is not conserved — it is simply redistributed away from legitimate long-term stakers toward the attacker.

---

### Likelihood Explanation

The attack requires only an unprivileged ingress sender with sufficient SNS token capital. No privileged role, admin key, or threshold corruption is needed. The attacker must observe the SNS governance canister for a proposal entering the `Open` state (publicly observable via query calls) and submit a stake transaction before the proposal's ballot snapshot is taken. On the Internet Computer, canister state is publicly observable, making proposal timing predictable. The economic barrier scales with the SNS's total staked supply; for smaller SNS instances, the required capital is modest. The 7-day maturity disbursement delay and the minimum dissolve delay are the only friction, and both are configurable per SNS.

---

### Recommendation

Implement a `VotingPowerSnapshots` mechanism in SNS governance analogous to the one in NNS governance (`rs/nns/governance/src/governance/voting_power_snapshots.rs`). Specifically:

1. Record periodic snapshots of total potential voting power in SNS governance.
2. In `compute_ballots_for_new_proposal`, compare the current total voting power against recent snapshots.
3. If a spike exceeding a configurable threshold (e.g., 1.5×) is detected, fall back to the snapshot with the minimum total potential voting power. [9](#0-8) [10](#0-9) 

---

### Proof of Concept

1. **Monitor**: Query the SNS governance canister for proposals entering `Open` state (publicly observable).
2. **Stake**: Immediately before a proposal is created (or just after it opens, before following neurons auto-vote), stake a large amount of SNS tokens and set a dissolve delay ≥ `neuron_minimum_dissolve_delay_to_vote_seconds`.
3. **Vote**: Configure the neuron to follow a neuron that votes `Yes` (or vote directly), ensuring the ballot is cast and `eligible_for_rewards()` returns true.
4. **Wait**: Wait for the reward round to complete (`round_duration_seconds`).
5. **Collect**: The attacker's neuron receives `rewards_purse_e8s × (attacker_voting_power / inflated_total_voting_power)` in maturity — a larger share than legitimate stakers expected.
6. **Exit**: Call `DisburseMaturity` (7-day delay), then dissolve the neuron after the minimum delay.
7. **Net result**: Attacker recovers principal plus captured maturity; all other voters received proportionally less maturity than they would have without the attack. [11](#0-10) [12](#0-11)

### Citations

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

**File:** rs/nns/governance/src/governance/voting_power_snapshots.rs (L35-65)
```rust
pub(crate) struct VotingPowerSnapshots {
    neuron_id_to_voting_power_maps:
        StableBTreeMap<TimestampSeconds, NeuronIdToVotingPowerMap, DefaultMemory>,
    voting_power_totals: StableBTreeMap<TimestampSeconds, VotingPowerTotal, DefaultMemory>,
}

fn insert_and_truncate<Value: Storable>(
    map: &mut StableBTreeMap<TimestampSeconds, Value, DefaultMemory>,
    timestamp_seconds: TimestampSeconds,
    value: Value,
) {
    let existing_value = map.insert(timestamp_seconds, value);

    // Log if we just clobbered an existing entry, because it is a exceedingly unlikely
    // that this would happen in practice.
    if let Some(existing_value) = existing_value {
        eprintln!(
            "{}Somehow the voting power snapshot is taken multiple times at \
	            the same timestamp {}",
            LOG_PREFIX, timestamp_seconds,
        );
    }

    // Drop earlier entries from map.
    while map.len() > MAX_VOTING_POWER_SNAPSHOTS {
        let (first_key, _) = map
            .first_key_value()
            .expect("No first key value even though the length is checked right before.");
        map.remove(&first_key);
    }
}
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

**File:** rs/sns/governance/src/governance.rs (L1680-1698)
```rust
        let now_seconds = self.env.now();
        let disbursement_in_progress = DisburseMaturityInProgress {
            amount_e8s: maturity_to_deduct,
            timestamp_of_disbursement_seconds: now_seconds,
            account_to_disburse_to: Some(to_account_proto),
            finalize_disbursement_timestamp_seconds: Some(
                now_seconds + MATURITY_DISBURSEMENT_DELAY_SECONDS,
            ),
        };

        // Re-borrow the neuron mutably to update now that the maturity has been
        // deducted and is waiting until the end of the window to modulate and disburse.
        let neuron = self.get_neuron_result_mut(id)?;
        neuron.maturity_e8s_equivalent = neuron
            .maturity_e8s_equivalent
            .saturating_sub(maturity_to_deduct);
        neuron
            .disburse_maturity_in_progress
            .push(disbursement_in_progress);
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

**File:** rs/sns/governance/src/governance.rs (L5509-5521)
```rust
        if should_distribute_rewards {
            match self.ledger.total_supply().await {
                Ok(supply) => {
                    // Distribute rewards
                    self.distribute_rewards(supply);
                }
                Err(e) => log!(
                    ERROR,
                    "Error when getting total governance token supply: {}",
                    GovernanceError::from(e)
                ),
            }
        }
```

**File:** rs/sns/governance/src/governance.rs (L5854-5875)
```rust
        let rewards_purse_e8s = {
            let mut result = Decimal::from(
                self.latest_reward_event()
                    .e8s_equivalent_to_be_rolled_over(),
            );
            let supply = i2d(supply.get_e8s());

            for i in 1..=new_rounds_count {
                let seconds_since_genesis = round_duration_seconds
                    .saturating_mul(i)
                    .saturating_add(reward_start_timestamp_seconds)
                    .saturating_sub(self.proto.genesis_timestamp_seconds);

                let current_reward_rate = voting_rewards_parameters.reward_rate_at(
                    crate::reward::Instant::from_seconds_since_genesis(i2d(seconds_since_genesis)),
                );

                result += current_reward_rate * voting_rewards_parameters.round_duration() * supply;
            }

            result
        };
```

**File:** rs/sns/governance/src/governance.rs (L5892-5934)
```rust
        // Add up reward shares based on voting power that was exercised.
        let mut neuron_id_to_reward_shares: HashMap<NeuronId, Decimal> = HashMap::new();
        for proposal_id in &considered_proposals {
            if let Some(proposal) = self.get_proposal_data(*proposal_id) {
                for (voter, ballot) in &proposal.ballots {
                    #[allow(clippy::blocks_in_conditions)]
                    if !Vote::try_from(ballot.vote)
                        .unwrap_or_else(|_| {
                            println!(
                                "{}Vote::from invoked with unexpected value {}.",
                                log_prefix(),
                                ballot.vote
                            );
                            Vote::Unspecified
                        })
                        .eligible_for_rewards()
                    {
                        continue;
                    }

                    match NeuronId::from_str(voter) {
                        Ok(neuron_id) => {
                            let reward_shares = i2d(ballot.voting_power);
                            *neuron_id_to_reward_shares
                                .entry(neuron_id)
                                .or_insert_with(|| dec!(0)) += reward_shares;
                        }
                        Err(e) => {
                            log!(
                                ERROR,
                                "Could not use voter {} to calculate total_voting_rights \
                                 since it's NeuronId was invalid. Underlying error: {:?}.",
                                voter,
                                e
                            );
                        }
                    }
                }
            }
        }
        // Freeze reward shares, now that we are done adding them up.
        let neuron_id_to_reward_shares = neuron_id_to_reward_shares;
        let total_reward_shares: Decimal = neuron_id_to_reward_shares.values().sum();
```

**File:** rs/sns/governance/src/governance.rs (L5946-5997)
```rust
        if total_reward_shares == dec!(0) {
            log!(
                ERROR,
                "Warning: total_reward_shares is 0. Therefore, we skip increasing \
                 neuron maturity. neuron_id_to_reward_shares: {:#?}",
                neuron_id_to_reward_shares,
            );
        } else {
            for (neuron_id, neuron_reward_shares) in neuron_id_to_reward_shares {
                let neuron: &mut Neuron = match self.get_neuron_result_mut(&neuron_id) {
                    Ok(neuron) => neuron,
                    Err(err) => {
                        log!(
                            ERROR,
                            "Cannot find neuron {}, despite having voted with power {} \
                             in the considered reward period. The reward that should have been \
                             distributed to this neuron is simply skipped, so the total amount \
                             of distributed reward for this period will be lower than the maximum \
                             allowed. Underlying error: {:?}.",
                            neuron_id,
                            neuron_reward_shares,
                            err
                        );
                        continue;
                    }
                };

                // Dividing before multiplying maximizes our chances of success.
                let neuron_reward_e8s =
                    rewards_purse_e8s * (neuron_reward_shares / total_reward_shares);

                // Round down, and convert to u64.
                let neuron_reward_e8s = u64::try_from(neuron_reward_e8s).unwrap_or_else(|err| {
                    panic!(
                        "Calculating reward for neuron {neuron_id:?}:\n\
                             neuron_reward_shares: {neuron_reward_shares}\n\
                             rewards_purse_e8s: {rewards_purse_e8s}\n\
                             total_reward_shares: {total_reward_shares}\n\
                             err: {err}",
                    )
                });
                // If the neuron has auto-stake-maturity on, add the new maturity to the
                // staked maturity, otherwise add it to the un-staked maturity.
                if neuron.auto_stake_maturity.unwrap_or(false) {
                    neuron.staked_maturity_e8s_equivalent = Some(
                        neuron.staked_maturity_e8s_equivalent.unwrap_or(0) + neuron_reward_e8s,
                    );
                } else {
                    neuron.maturity_e8s_equivalent += neuron_reward_e8s;
                }
                distributed_e8s_equivalent += neuron_reward_e8s;
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
