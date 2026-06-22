### Title
Voting Reward Distribution Rounding Loss Is Never Rolled Over, Causing Permanent Maturity Deficit - (`rs/nns/governance/src/governance.rs`, `rs/sns/governance/src/governance.rs`)

---

### Summary

In both NNS and SNS governance, the voting rewards pool (`total_available_e8s_equivalent`) is computed each round, but the sum of per-neuron maturity increments (`distributed_e8s_equivalent`) is always strictly less than the pool due to floor truncation. When proposals are settled, the undistributed remainder is silently discarded — it is never rolled over to the next round and never minted. This is the IC analog of the external report's "unclaimable reserve assets" pattern: two related accounting paths (pool computation vs. per-neuron distribution) use asymmetric precision, and the gap permanently disappears.

---

### Finding Description

**NNS Governance (`rs/nns/governance/src/governance.rs`)**

The rewards pool is computed in floating-point:

```rust
let total_available_e8s_equivalent_float = (supply.get_e8s() as f64) * fraction
    + rolling_over_from_previous_reward_event_e8s_equivalent as f64;
```

Each neuron's reward is then individually floor-truncated to `u64`:

```rust
let reward = (used_voting_rights * total_available_e8s_equivalent_float
    / total_voting_rights) as u64;
actually_distributed_e8s_equivalent += reward;
```

The `RewardEvent` records both values:

```rust
distributed_e8s_equivalent: actually_distributed_e8s_equivalent,
total_available_e8s_equivalent: total_available_e8s_equivalent_float as u64,
```

**The rollover gate** in `rs/nns/governance/src/reward/calculation.rs` only carries the pool forward when there are **no** settled proposals:

```rust
pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
    if self.rewards_rolled_over() {   // true only when settled_proposals.is_empty()
        self.total_available_e8s_equivalent
    } else {
        0   // <-- undistributed remainder is dropped every normal round
    }
}
```

In every normal round (proposals settled), the gap `total_available_e8s_equivalent − distributed_e8s_equivalent` is set to zero and never recovered.

**SNS Governance (`rs/sns/governance/src/governance.rs`)** has the identical pattern. The rewards purse is a `Decimal`, each neuron's share is floor-truncated via `u64::try_from`, and the code itself acknowledges the loss:

```rust
// Because of rounding (and other shenanigans), it is possible that some
// portion of this amount ends up not being actually distributed.
```

The `debug_assert` confirms the invariant:

```rust
debug_assert!(
    i2d(distributed_e8s_equivalent) <= rewards_purse_e8s, ...
);
```

The gap is never rolled over; `e8s_equivalent_to_be_rolled_over()` in `rs/sns/governance/src/types.rs` returns 0 whenever `settled_proposals` is non-empty.

---

### Impact Explanation

Every reward round in which at least one proposal is settled permanently destroys a small amount of maturity. The loss per round is bounded by `N` e8s (one e8 per voting neuron) plus the fractional part of the float-to-u64 truncation of the pool itself. With ~100,000 NNS neurons and daily rounds, the maximum annual loss is on the order of 100,000 e8s/day × 365 ≈ 0.365 ICP/year. For SNS DAOs with high-value governance tokens, the dollar-equivalent loss per year can exceed the $10 threshold. The maturity is not locked in a contract — it simply ceases to exist: it is never credited to any neuron and never minted. There is no recovery path (no skim function, no admin rescue).

---

### Likelihood Explanation

This occurs unconditionally on every reward round where at least one proposal is settled, which is the normal operating mode for both NNS and SNS. The trigger is the routine `distribute_voting_rewards_to_neurons` / `distribute_rewards` heartbeat call. Any governance participant who votes on a proposal indirectly triggers the loss. No special conditions, no attacker required.

---

### Recommendation

When proposals are settled and rewards are distributed, carry the undistributed remainder (`total_available_e8s_equivalent − distributed_e8s_equivalent`) forward into the next round's pool, analogous to how the full pool is rolled over when no proposals are settled. Concretely, change `e8s_equivalent_to_be_rolled_over` to return the remainder instead of 0 in the non-rollover case, or add a separate `undistributed_remainder` field that is always added to the next round's starting pool.

---

### Proof of Concept

Consider a single reward round with:
- `total_available_e8s_equivalent_float` = 1,000,000.7 (float)
- 3 neurons with equal voting power

Each neuron receives `floor(1,000,000.7 / 3)` = `floor(333,333.567)` = `333,333` e8s.  
`distributed_e8s_equivalent` = 999,999.  
`total_available_e8s_equivalent` (recorded) = 1,000,000 (float truncated).  
Gap = 1 e8 (from float truncation) + 1 e8 (from per-neuron rounding) = 2 e8s.

`e8s_equivalent_to_be_rolled_over()` returns **0** because `settled_proposals` is non-empty. The 2 e8s are permanently lost. Repeated every day across 100,000 neurons, the cumulative annual loss is material.

---

**Root cause files:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** rs/nns/governance/src/governance.rs (L6724-6752)
```rust
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

        let reward_event = RewardEvent {
            day_after_genesis,
            actual_timestamp_seconds: now,
            settled_proposals: considered_proposals,
            distributed_e8s_equivalent: actually_distributed_e8s_equivalent,
            total_available_e8s_equivalent: total_available_e8s_equivalent_float as u64,
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

**File:** rs/sns/governance/src/governance.rs (L5940-6006)
```rust
        // Because of rounding (and other shenanigans), it is possible that some
        // portion of this amount ends up not being actually distributed.
        let mut distributed_e8s_equivalent = 0_u64;
        // Now that we know the size of the pie (rewards_purse_e8s), and how
        // much of it each neuron is supposed to get (*_reward_shares), we now
        // proceed to actually handing out those rewards.
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
        }
        // Freeze distributed_e8s_equivalent, now that we are done handing out rewards.
        let distributed_e8s_equivalent = distributed_e8s_equivalent;
        // Because we used floor to round rewards to integers (and everything is
        // non-negative), it should be that the amount distributed is not more
        // than the original purse.
        debug_assert!(
            i2d(distributed_e8s_equivalent) <= rewards_purse_e8s,
            "rewards distributed ({distributed_e8s_equivalent}) > purse ({rewards_purse_e8s})",
```

**File:** rs/sns/governance/src/types.rs (L2054-2059)
```rust
    pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
        if self.rewards_rolled_over() {
            self.total_available_e8s_equivalent.unwrap_or_default()
        } else {
            0
        }
```
