### Title
SNS `compute_ballots_for_new_proposal` Uses Spot Voting Power Without Spike Detection, Enabling Sudden Governance Takeover - (File: rs/sns/governance/src/governance.rs)

### Summary

The SNS governance canister computes ballot voting power at proposal-creation time using the current (spot) `cached_neuron_stake_e8s` of every neuron, with no historical comparison or spike-detection guard. The NNS governance canister recognized this exact class of risk and added a `VotingPowerSnapshots` mechanism that falls back to a previous snapshot when the current total potential voting power exceeds 1.5× the minimum of the last seven daily snapshots. The SNS canister has no equivalent protection. An unprivileged actor who acquires a large block of SNS tokens can stake them, immediately refresh the neuron, and submit a proposal whose ballot already reflects the full inflated stake — enough to reach the early-adoption threshold and execute the proposal before the community can react.

### Finding Description

`rs/sns/governance/src/governance.rs` `compute_ballots_for_new_proposal` iterates over every neuron and calls `v.voting_power(now_seconds, …)`, which internally reads `self.voting_power_stake_e8s()` — a function that returns `cached_neuron_stake_e8s - neuron_fees_e8s + staked_maturity`. [1](#0-0) 

`cached_neuron_stake_e8s` is updated to the live ledger balance whenever `refresh_neuron` is called: [2](#0-1) [3](#0-2) 

There is no historical snapshot, no time-weighted average, and no spike-detection guard anywhere in `compute_ballots_for_new_proposal`. [4](#0-3) 

By contrast, the NNS governance canister maintains a `VotingPowerSnapshots` store (updated by a daily timer task) and, at proposal creation, checks whether the current total potential voting power exceeds 1.5× the minimum of the retained snapshots. If a spike is detected, it substitutes the historical snapshot for the current one: [5](#0-4) [6](#0-5) [7](#0-6) 

The SNS canister has no `VotingPowerSnapshots`, no `SnapshotVotingPowerTask`, and no call to any equivalent guard inside `compute_ballots_for_new_proposal`.

### Impact Explanation

An attacker who controls a large block of SNS tokens can:

1. Transfer tokens to a neuron subaccount (or top up an existing neuron).
2. Call `manage_neuron { ClaimOrRefresh }` to update `cached_neuron_stake_e8s` to the full inflated balance.
3. Immediately call `manage_neuron { MakeProposal }` — the ballot for their neuron is assigned the full inflated voting power at that instant.
4. If the inflated stake exceeds 50 % of total voting power, the proposal is adopted in the same round via the early-adoption path.
5. The attacker then begins dissolving the neuron; after the dissolve delay the tokens are returned.

Possible proposal payloads include: upgrading the SNS governance or root canister to attacker-controlled code, draining the SNS treasury, or changing `NervousSystemParameters` (e.g., setting `neuron_minimum_dissolve_delay_to_vote_seconds` to zero for future attacks). The SNS root canister can upgrade any dapp canister registered with the SNS, so a successful governance takeover gives the attacker full control of the dapp.

### Likelihood Explanation

The entry path requires no privileged role — any principal can stake SNS tokens and submit proposals. The capital requirement scales with the existing total staked voting power, but for smaller or newly launched SNS DAOs the threshold can be modest. The attack is fully on-chain and deterministic: stake → refresh → propose → execute. No social engineering, no threshold corruption, and no external oracle is involved. The NNS team explicitly recognized this risk class and shipped the spike-detection mechanism (Proposal 137252); the SNS canister has not received the equivalent fix.

### Recommendation

Port the NNS `VotingPowerSnapshots` / `SnapshotVotingPowerTask` pattern to the SNS governance canister:

1. Add a periodic timer task that records a daily snapshot of total potential voting power.
2. In `compute_ballots_for_new_proposal`, compare the current total against the minimum of the retained snapshots.
3. If the ratio exceeds a configurable threshold (e.g., 1.5×), substitute the historical snapshot's per-neuron voting power for the current one, preventing sudden large stakes from immediately dominating a vote.

As a shorter-term measure, enforce a minimum waiting period between the last `refresh_neuron` call and proposal eligibility, or require that a neuron's stake has been at its current level for at least one snapshot interval before it contributes to early adoption.

### Proof of Concept

**Step 1 – Attacker stakes a dominant position.**

```
// Attacker transfers 60 % of circulating SNS tokens to their neuron subaccount.
// Neuron already has dissolve_delay >= neuron_minimum_dissolve_delay_to_vote_seconds.
ledger.icrc1_transfer({ to: neuron_subaccount, amount: large_amount });
```

**Step 2 – Refresh neuron to update cached stake.**

```
sns_governance.manage_neuron({
    subaccount: attacker_subaccount,
    command: ClaimOrRefresh { by: NeuronId {} }
});
// cached_neuron_stake_e8s is now set to large_amount via refresh_neuron → update_stake
```

**Step 3 – Submit malicious proposal.**

```
sns_governance.manage_neuron({
    subaccount: attacker_subaccount,
    command: MakeProposal { ... upgrade_sns_controlled_canister ... }
});
// compute_ballots_for_new_proposal reads cached_neuron_stake_e8s = large_amount
// attacker ballot voting_power >> 50 % of total → early adoption fires immediately
```

**Step 4 – Recover tokens.**

```
// Attacker starts dissolving the neuron; tokens returned after dissolve_delay.
sns_governance.manage_neuron({ command: Configure { StartDissolving {} } });
```

The root cause — spot `cached_neuron_stake_e8s` read with no historical guard — is at: [8](#0-7) [9](#0-8)

### Citations

**File:** rs/sns/governance/src/governance.rs (L4237-4295)
```rust
    async fn refresh_neuron(&mut self, nid: &NeuronId) -> Result<(), GovernanceError> {
        let now = self.env.now();
        let subaccount = nid.subaccount()?;
        let account = self.neuron_account_id(subaccount);

        // First ensure that the neuron was not created via an NNS Neurons' Fund participation in the
        // decentralization swap
        {
            let neuron = self.get_neuron_result(nid)?;

            if neuron.is_neurons_fund_controlled() {
                return Err(GovernanceError::new_with_message(
                    ErrorType::PreconditionFailed,
                    "Cannot refresh an SNS Neuron controlled by the Neurons' Fund",
                ));
            }
        }

        // Get the balance of the neuron from the ledger canister.
        let balance = self.ledger.account_balance(account).await?;

        let min_stake = self
            .nervous_system_parameters_or_panic()
            .neuron_minimum_stake_e8s
            .expect("NervousSystemParameters must have neuron_minimum_stake_e8s");
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
        let neuron = self.get_neuron_result_mut(nid)?;
        match neuron.cached_neuron_stake_e8s.cmp(&balance.get_e8s()) {
            Ordering::Greater => {
                log!(
                    ERROR,
                    "ERROR. Neuron cached stake was inconsistent.\
                     Neuron account: {} has less e8s: {} than the cached neuron stake: {}.\
                     Stake adjusted.",
                    account,
                    balance.get_e8s(),
                    neuron.cached_neuron_stake_e8s
                );
                neuron.update_stake(balance.get_e8s(), now);
            }
            Ordering::Less => {
                neuron.update_stake(balance.get_e8s(), now);
            }
            // If the stake is the same as the account balance,
            // just return the neuron id (this way this method
            // also serves the purpose of allowing to discover the
            // neuron id based on the memo and the controller).
            Ordering::Equal => (),
        };
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

**File:** rs/sns/governance/src/neuron.rs (L641-645)
```rust
    fn voting_power_stake_e8s(&self) -> u64 {
        self.cached_neuron_stake_e8s
            .saturating_sub(self.neuron_fees_e8s)
            .saturating_add(self.staked_maturity_e8s_equivalent.unwrap_or(0))
    }
```
