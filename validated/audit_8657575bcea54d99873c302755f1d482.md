### Title
First Voter Claims All Rolled-Over SNS Voting Rewards When No Neurons Were Staked - (`rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS governance `distribute_rewards` function accumulates a rewards purse proportional to the **total token supply** regardless of whether any neurons are staked. When no proposals are settled in a round, the entire purse rolls over. An attacker who stakes the first neuron after a period of no staked neurons (or no proposals) can vote on a single proposal and claim the entire accumulated rolled-over rewards purse.

---

### Finding Description

In `rs/sns/governance/src/governance.rs`, `distribute_rewards` computes the rewards purse each round as:

```
rewards_purse_e8s += current_reward_rate * round_duration * total_token_supply
``` [1](#0-0) 

The `total_token_supply` here is the **entire circulating supply** fetched from the ledger, not the staked supply. There is no guard that skips reward accrual when no neurons are staked.

When `considered_proposals` is empty (no proposals ready to settle), `total_reward_shares == 0`, so no maturity is distributed and the full purse is stored in `total_available_e8s_equivalent`: [2](#0-1) 

The rollover predicate is:

```rust
pub(crate) fn rewards_rolled_over(&self) -> bool {
    self.settled_proposals.is_empty()
}
``` [3](#0-2) 

And the rolled-over amount is fed back into the next round's purse:

```rust
let mut result = Decimal::from(
    self.latest_reward_event()
        .e8s_equivalent_to_be_rolled_over(),
);
``` [4](#0-3) 

This means: if an SNS runs for N rounds with no settled proposals (because no neurons are staked, or no proposals are made), the rewards purse grows to approximately `N * rate * round_duration * total_supply`. The first neuron that votes on any proposal receives the **entire** accumulated purse.

The `e8s_equivalent_to_be_rolled_over` implementation in SNS: [5](#0-4) 

---

### Impact Explanation

An attacker who stakes the minimum-stake neuron after a long period of no staked neurons (or no proposals) can:

1. Stake a neuron with `neuron_minimum_stake_e8s` tokens.
2. Submit a proposal (paying `proposal_reject_cost_e8s`).
3. Vote on it (the neuron is the only voter, so it has 100% of voting power).
4. Wait for the reward round to end — `distribute_rewards` is called from `run_periodic_tasks`.
5. Receive `maturity_e8s_equivalent` equal to the entire accumulated rolled-over purse, which can be orders of magnitude larger than the attacker's stake.
6. Disburse the maturity via `DisburseMaturity`.

The attacker's cost is `neuron_minimum_stake_e8s + proposal_reject_cost_e8s` (if the proposal is rejected). The gain is the entire rolled-over rewards purse, which grows linearly with the number of empty rounds. [6](#0-5) 

---

### Likelihood Explanation

This is realistic in several concrete scenarios:

1. **SNS deployed without a swap**: An SNS can be initialized with no initial neurons. Rewards begin accruing from `genesis_timestamp_seconds` immediately.
2. **Gap between SNS genesis and swap completion**: The swap canister calls `claim_swap_neurons` only after the swap commits. During the swap period, no neurons may exist in governance yet, but rewards accrue.
3. **All neurons dissolved**: If all SNS neurons dissolve and no one re-stakes, the purse accumulates indefinitely until the first new staker.
4. **Long proposal drought**: Even with existing neurons, if no proposals are made for many rounds, the purse rolls over and the next voter claims it all.

The attack requires no privileged access — any principal can stake a neuron and submit a proposal via the public `manage_neuron` endpoint. [7](#0-6) 

---

### Recommendation

Add a guard in `distribute_rewards` to skip reward accrual when no neurons are staked (analogous to the wxETH fix of checking `totalSupply == 0`):

```rust
// In distribute_rewards, before computing rewards_purse_e8s:
let total_staked_e8s: u64 = self.proto.neurons.values()
    .map(|n| n.cached_neuron_stake_e8s)
    .sum();
if total_staked_e8s == 0 {
    // No neurons staked; do not accrue rewards this round.
    // Update latest_reward_event to advance the round counter without rolling over.
    return;
}
```

Alternatively, do not roll over the purse when `total_staked_e8s == 0` — discard it instead, so that rewards only accumulate during periods when there is actual staked supply to distribute to.

---

### Proof of Concept

**Setup**: Deploy an SNS with `initial_reward_rate_basis_points = 500` (5%), `round_duration_seconds = 86400` (1 day), and no initial neurons.

**Step 1**: Let 30 days pass. Each day, `run_periodic_tasks` calls `distribute_rewards`. Since `considered_proposals` is empty every round, the purse rolls over. After 30 days:

```
rewards_purse ≈ 30 * (5%/365) * total_supply
             ≈ 0.41% of total_supply
```

**Step 2**: Attacker stakes 1 neuron with `neuron_minimum_stake_e8s = 100_000_000` (1 SNS token) and sets dissolve delay to `neuron_minimum_dissolve_delay_to_vote_seconds`.

**Step 3**: Attacker submits a `Motion` proposal (cost: `proposal_reject_cost_e8s`). Since the attacker's neuron is the only neuron, it has 100% of voting power. The proposal settles at the end of the voting period.

**Step 4**: `distribute_rewards` runs. `considered_proposals` is now non-empty. `total_reward_shares = attacker_voting_power`. The attacker receives:

```
neuron_reward_e8s = rewards_purse_e8s * (attacker_shares / total_shares)
                  = rewards_purse_e8s * 1.0
                  = entire 30-day accumulated purse
``` [8](#0-7) 

**Step 5**: Attacker calls `DisburseMaturity` to convert the maturity to tokens, receiving far more than the initial stake.

The root cause — reward accrual independent of staked supply — is confirmed at: [9](#0-8)

### Citations

**File:** rs/sns/governance/src/governance.rs (L4319-4408)
```rust
    async fn claim_neuron(
        &mut self,
        neuron_id: NeuronId,
        principal_id: &PrincipalId,
    ) -> Result<(), GovernanceError> {
        let now = self.env.now();

        // We need to create the neuron before checking the balance so that we record
        // the neuron and add it to the set of neurons with ongoing operations. This
        // avoids a race where a user calls this method a second time before the first
        // time responds. If we store the neuron and lock it before we make the call,
        // we know that any concurrent call to mutate the same neuron will need to wait
        // for this one to finish before proceeding.
        let neuron = Neuron {
            id: Some(neuron_id.clone()),
            permissions: vec![NeuronPermission::new(
                principal_id,
                self.neuron_claimer_permissions_or_panic().permissions,
            )],
            cached_neuron_stake_e8s: 0,
            neuron_fees_e8s: 0,
            created_timestamp_seconds: now,
            aging_since_timestamp_seconds: now,
            followees: self.default_followees_or_panic().followees,
            topic_followees: Some(TopicFollowees {
                topic_id_to_followees: btreemap! {},
            }),
            maturity_e8s_equivalent: 0,
            dissolve_state: Some(DissolveState::DissolveDelaySeconds(0)),
            // A neuron created through the `claim_or_refresh` ManageNeuron command will
            // have the default voting power multiplier applied.
            voting_power_percentage_multiplier: DEFAULT_VOTING_POWER_PERCENTAGE_MULTIPLIER,
            source_nns_neuron_id: None,
            staked_maturity_e8s_equivalent: None,
            auto_stake_maturity: None,
            vesting_period_seconds: None,
            disburse_maturity_in_progress: vec![],
        };

        // This also verifies that there are not too many neurons already.
        self.add_neuron(neuron.clone())?;

        // Get the balance of the neuron's subaccount from ledger canister.
        let subaccount = neuron_id.subaccount()?;
        let account = self.neuron_account_id(subaccount);
        let balance = self.ledger.account_balance(account).await?;

        let min_stake = self
            .nervous_system_parameters_or_panic()
            .neuron_minimum_stake_e8s
            .expect("NervousSystemParameters must have neuron_minimum_stake_e8s");

        if balance.get_e8s() < min_stake {
            // To prevent this method from creating non-staked
            // neurons, we must also remove the neuron that was
            // previously created.
            self.remove_neuron(&neuron_id, neuron)?;
            return Err(GovernanceError::new_with_message(
                ErrorType::InsufficientFunds,
                format!(
                    "Account does not have enough funds to stake a neuron. \
                     Please make sure that account has at least {:?} e8s (was {:?} e8s)",
                    min_stake,
                    balance.get_e8s()
                ),
            ));
        }

        // Ok, we are able to stake the neuron.
        match self.get_neuron_result_mut(&neuron_id) {
            Ok(neuron) => {
                // Adjust the stake.
                neuron.update_stake(balance.get_e8s(), now);
                Ok(())
            }
            Err(err) => {
                // This should not be possible, but let's be defensive and provide a
                // reasonable error message, but still panic so that the lock remains
                // acquired and we can investigate.
                panic!(
                    "When attempting to stake a neuron with ID {} and stake {:?},\
                     the neuron disappeared while the operation was in flight.\
                     The returned error was: {}",
                    neuron_id,
                    balance.get_e8s(),
                    err
                )
            }
        }
    }
```

**File:** rs/sns/governance/src/governance.rs (L5503-5521)
```rust
        let should_distribute_rewards = self.should_distribute_rewards();

        // Getting the total governance token supply from the ledger is expensive enough
        // that we don't want to do it on every call to `run_periodic_tasks`. So
        // we only fetch it when it's needed, which is when rewards should be
        // distributed
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

**File:** rs/sns/governance/src/governance.rs (L5946-5953)
```rust
        if total_reward_shares == dec!(0) {
            log!(
                ERROR,
                "Warning: total_reward_shares is 0. Therefore, we skip increasing \
                 neuron maturity. neuron_id_to_reward_shares: {:#?}",
                neuron_id_to_reward_shares,
            );
        } else {
```

**File:** rs/sns/governance/src/governance.rs (L5973-5996)
```rust
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
```

**File:** rs/sns/governance/src/types.rs (L2054-2060)
```rust
    pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
        if self.rewards_rolled_over() {
            self.total_available_e8s_equivalent.unwrap_or_default()
        } else {
            0
        }
    }
```

**File:** rs/sns/governance/src/types.rs (L2064-2067)
```rust
    /// Whether this is a "rollover event", where no rewards were distributed.
    pub(crate) fn rewards_rolled_over(&self) -> bool {
        self.settled_proposals.is_empty()
    }
```
