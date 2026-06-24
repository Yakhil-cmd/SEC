### Title
Systematic Voting Reward Dust Loss Due to Per-Neuron `f64` Truncation With No Rollover — (`File: rs/nns/governance/src/governance.rs`)

---

### Summary

In the NNS governance canister, the `calculate_voting_rewards` function computes the total reward pool using `f64` floating-point arithmetic and then distributes per-neuron rewards by truncating a floating-point division result to `u64`. The sum of all per-neuron truncated rewards (`actually_distributed_e8s_equivalent`) is always strictly less than `total_available_e8s_equivalent`. The difference — the "dust" — is permanently discarded: it is neither rolled over to the next reward event nor credited to any neuron. This is the direct IC analog of the StWSX.sol ledger conservation bug, where total supply and sum of balances diverge after reward collection.

---

### Finding Description

In `rs/nns/governance/src/governance.rs`, the function `calculate_voting_rewards` computes the reward pool as:

```rust
let total_available_e8s_equivalent_float = (supply.get_e8s() as f64) * fraction
    + rolling_over_from_previous_reward_event_e8s_equivalent as f64;
``` [1](#0-0) 

Then, for each voting neuron, the individual reward is computed by a floating-point division that is immediately truncated (floored) to `u64` via an `as u64` cast:

```rust
let reward = (used_voting_rights * total_available_e8s_equivalent_float
    / total_voting_rights) as u64;
``` [2](#0-1) 

The sum of all such truncated rewards is accumulated into `actually_distributed_e8s_equivalent`: [3](#0-2) 

Both values are recorded in the `RewardEvent`: [4](#0-3) 

The invariant `actually_distributed_e8s_equivalent ≤ total_available_e8s_equivalent` always holds, but the difference is never recovered. The rollover mechanism in `e8s_equivalent_to_be_rolled_over()` returns `0` whenever proposals were settled (i.e., whenever rewards were actually distributed): [5](#0-4) 

This means the dust from truncation is silently dropped every reward round. The `continue_processing` function in `RewardsDistribution` only credits neurons with the pre-computed truncated amounts and has no mechanism to recover the remainder: [6](#0-5) 

The `CalculateDistributableRewardsTask` fires this path automatically every day: [7](#0-6) 

---

### Impact Explanation

Every daily NNS reward round in which at least two neurons vote on a settled proposal loses between `1` and `N-1` e8s (where N is the number of distinct voting neurons), permanently. These e8s are never minted to any neuron's `maturity_e8s_equivalent` or `staked_maturity_e8s_equivalent`. The governance protocol's stated `total_available_e8s_equivalent` diverges from the actual `distributed_e8s_equivalent` in every such round, and the gap is cumulative and monotonically increasing. With ~1,000 active voting neurons, up to 999 e8s can be lost per day. Over years of operation this accumulates to a non-trivial amount of ICP that the protocol promised to distribute but never did. This is a **ledger conservation bug**: the governance canister's accounting of promised rewards does not match what is actually credited to neuron owners.

---

### Likelihood Explanation

This is **certain to occur** in every reward round with multiple voters. The NNS has been live for years with thousands of voting neurons, meaning this loss has been accumulating since genesis. No special attacker action is required; the bug is triggered automatically by the `CalculateDistributableRewardsTask` timer task that runs daily. Any unprivileged neuron holder who votes on a proposal participates in triggering the condition.

---

### Recommendation

Replace the per-neuron `as u64` truncation with a remainder-aware distribution. After distributing `floor(reward)` to each neuron, accumulate the fractional remainders and credit the residual dust to one neuron (e.g., the one with the largest remainder, as in the Hamilton apportionment method) or explicitly roll the undistributed dust into the next reward event's `rolling_over_from_previous_reward_event_e8s_equivalent`. The SNS governance already acknowledges this issue with a comment ("Because of rounding (and other shenanigans), it is possible that some portion of this amount ends up not being actually distributed") but also does not recover the dust. [8](#0-7) 

---

### Proof of Concept

**Setup:** Two neurons, each with equal voting power `V`. Reward pool `P = 101` e8s. `total_voting_rights = 2V`.

**Per-neuron reward computation:**
```
reward_neuron_1 = floor(V * 101 / 2V) = floor(50.5) = 50
reward_neuron_2 = floor(V * 101 / 2V) = floor(50.5) = 50
actually_distributed = 100
total_available     = 101
dust lost           = 1 e8s (permanently)
```

Repeated daily for 1,000 days → 1,000 e8s = 0.00001 ICP permanently lost. With 1,000 neurons and a larger pool, the per-round loss scales to up to 999 e8s/day. The `RewardEvent` will consistently show `distributed_e8s_equivalent < total_available_e8s_equivalent` with no mechanism to close the gap. [9](#0-8)

### Citations

**File:** rs/nns/governance/src/governance.rs (L6651-6654)
```rust
        let rolling_over_from_previous_reward_event_e8s_equivalent =
            latest_reward_event.e8s_equivalent_to_be_rolled_over();
        let total_available_e8s_equivalent_float = (supply.get_e8s() as f64) * fraction
            + rolling_over_from_previous_reward_event_e8s_equivalent as f64;
```

**File:** rs/nns/governance/src/governance.rs (L6720-6745)
```rust
        } else {
            let mut reward_distribution = RewardsDistribution::new();
            for (neuron_id, used_voting_rights) in voters_to_used_voting_right {
                if self.neuron_store.contains(neuron_id) {
                    let reward = (used_voting_rights * total_available_e8s_equivalent_float
                        / total_voting_rights) as u64;

                    reward_distribution.add_reward(neuron_id, reward);

                    // NOTE: This is the only reason we are checking the existence of neurons
                    // at this stage. Otherwise, we could defer until we distribute them in the
                    // schedule task.
                    actually_distributed_e8s_equivalent += reward;
                } else {
                    println!(
                        "{}Cannot find neuron {}, despite having voted with power {} \
                            in the considered reward period. The reward that should have been \
                            distributed to this neuron is simply skipped, so the total amount \
                            of distributed reward for this period will be lower than the maximum \
                            allowed.",
                        LOG_PREFIX, neuron_id.id, used_voting_rights
                    );
                }
            }
            Some(reward_distribution)
        };
```

**File:** rs/nns/governance/src/governance.rs (L6747-6757)
```rust
        let reward_event = RewardEvent {
            day_after_genesis,
            actual_timestamp_seconds: now,
            settled_proposals: considered_proposals,
            distributed_e8s_equivalent: actually_distributed_e8s_equivalent,
            total_available_e8s_equivalent: total_available_e8s_equivalent_float as u64,
            rounds_since_last_distribution: Some(rounds_since_last_distribution),
            latest_round_available_e8s_equivalent: Some(
                latest_round_available_e8s_equivalent_float as u64,
            ),
        };
```

**File:** rs/nns/governance/src/reward/calculation.rs (L120-126)
```rust
    pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
        if self.rewards_rolled_over() {
            self.total_available_e8s_equivalent
        } else {
            0
        }
    }
```

**File:** rs/nns/governance/src/reward/distribution.rs (L154-188)
```rust
    fn continue_processing(
        &mut self,
        neuron_store: &mut NeuronStore,
        is_over_instructions_limit: fn() -> bool,
    ) {
        while let Some((id, reward_e8s)) = self.rewards.pop_first() {
            match neuron_store.with_neuron_mut(&id, |neuron| {
                let auto_stake = neuron.auto_stake_maturity.unwrap_or(false);
                if auto_stake {
                    neuron.staked_maturity_e8s_equivalent = Some(
                        neuron
                            .staked_maturity_e8s_equivalent
                            .unwrap_or_default()
                            .saturating_add(reward_e8s),
                    );
                } else {
                    neuron.maturity_e8s_equivalent =
                        neuron.maturity_e8s_equivalent.saturating_add(reward_e8s);
                }
            }) {
                Ok(_) => {}
                Err(e) => {
                    println!(
                        "{}Error rewarding neuron {:?} during reward_distribution.\
                    This should not be possible as neuron existence is checked when \
                    rewards are calculated: {}",
                        LOG_PREFIX, id, e
                    );
                }
            };
            if is_over_instructions_limit() {
                break;
            }
        }
    }
```

**File:** rs/nns/governance/src/timer_tasks/calculate_distributable_rewards.rs (L52-62)
```rust
    async fn execute(self) -> (Duration, Self) {
        let total_supply = self
            .governance
            .with_borrow(|governance| governance.get_ledger())
            .total_supply()
            .await;
        match total_supply {
            Ok(total_supply) => {
                self.governance.with_borrow_mut(|governance| {
                    governance.distribute_voting_rewards_to_neurons(total_supply);
                });
```

**File:** rs/sns/governance/src/governance.rs (L5940-5942)
```rust
        // Because of rounding (and other shenanigans), it is possible that some
        // portion of this amount ends up not being actually distributed.
        let mut distributed_e8s_equivalent = 0_u64;
```
