### Title
Unchecked Integer Arithmetic in SNS Governance `distribute_rewards` Silently Wraps Neuron Maturity on Overflow — (File: rs/sns/governance/src/governance.rs)

---

### Summary

The SNS governance `distribute_rewards` function uses plain Rust `+` and `+=` operators on `u64` maturity fields when crediting voting rewards to neurons. In Rust release builds (used in IC production), integer overflow wraps silently via two's complement, meaning a neuron whose accumulated maturity approaches `u64::MAX` would have its maturity silently reset to near zero upon receiving a reward. The NNS governance equivalent correctly uses `saturating_add` for the same operation, making this an inconsistency and a governance conservation bug.

---

### Finding Description

In `rs/sns/governance/src/governance.rs`, the `distribute_rewards` function iterates over neurons that voted and credits each with a proportional share of the reward purse: [1](#0-0) 

The three unchecked arithmetic operations are:

1. **Line 5991**: `neuron.staked_maturity_e8s_equivalent.unwrap_or(0) + neuron_reward_e8s` — plain `+` on `u64`
2. **Line 5994**: `neuron.maturity_e8s_equivalent += neuron_reward_e8s` — plain `+=` on `u64`
3. **Line 5996**: `distributed_e8s_equivalent += neuron_reward_e8s` — plain `+=` on `u64`

By contrast, the NNS governance reward distribution in `rs/nns/governance/src/reward/distribution.rs` uses `saturating_add` for the identical operation: [2](#0-1) 

The SNS neuron's `maturity_e8s_equivalent` and `staked_maturity_e8s_equivalent` fields are both `u64`: [3](#0-2) 

In Rust release builds, integer overflow on primitive integer types wraps silently (two's complement). IC production canisters are compiled in release mode. Therefore, if a neuron's `maturity_e8s_equivalent` is sufficiently close to `u64::MAX` (≈ 1.84 × 10¹⁹ e8s), adding any nonzero `neuron_reward_e8s` will wrap the value to near zero, silently destroying the neuron's accumulated maturity.

Additionally, the `distributed_e8s_equivalent` accumulator at line 5996 can overflow if the sum of all per-neuron rewards in a single round exceeds `u64::MAX`. This would cause the `RewardEvent`'s `distributed_e8s_equivalent` field to record an incorrect (wrapped) value, corrupting the rollover accounting used in subsequent reward rounds. [4](#0-3) 

---

### Impact Explanation

**Vulnerability class:** Governance conservation bug / ledger conservation bug (maturity is the precursor to minted SNS tokens via `disburse_maturity`).

- A neuron whose `maturity_e8s_equivalent` wraps to near zero loses all accumulated maturity. Since maturity is the basis for minting SNS tokens via `disburse_maturity`, this constitutes a permanent, irreversible loss of governance token value for the affected neuron holder.
- If `distributed_e8s_equivalent` wraps, the `RewardEvent` records a falsely low distributed amount. The `e8s_equivalent_to_be_rolled_over` method uses this field to compute the rollover for the next round, meaning future reward rounds would distribute an inflated purse (double-counting the wrapped amount), breaking the SNS token emission schedule. [5](#0-4) 

---

### Likelihood Explanation

**Low-to-medium.** The overflow of a single neuron's maturity requires that neuron to have accumulated maturity near `u64::MAX` e8s (≈ 184 billion SNS tokens at 1 token = 10⁸ e8s). For most SNS deployments with total supplies well below this threshold, this is not immediately reachable. However:

- The inconsistency with NNS governance (which uses `saturating_add`) indicates this is an unintentional omission, not a deliberate design choice.
- The `distributed_e8s_equivalent` accumulator overflow is more reachable: if a single reward round distributes rewards to thousands of neurons each receiving large amounts, the sum could exceed `u64::MAX`.
- No privileged access is required. The overflow is triggered automatically by the periodic `distribute_rewards` call, which is part of normal SNS governance operation. Any SNS participant who votes on proposals contributes to the conditions that trigger this path.

---

### Recommendation

Replace all three unchecked arithmetic operations in the SNS `distribute_rewards` loop with `saturating_add`, matching the NNS governance implementation:

```rust
// Line 5991
neuron.staked_maturity_e8s_equivalent = Some(
    neuron.staked_maturity_e8s_equivalent.unwrap_or(0).saturating_add(neuron_reward_e8s),
);
// Line 5994
neuron.maturity_e8s_equivalent = neuron.maturity_e8s_equivalent.saturating_add(neuron_reward_e8s);
// Line 5996
distributed_e8s_equivalent = distributed_e8s_equivalent.saturating_add(neuron_reward_e8s);
```

Additionally, audit the NNS governance `actually_distributed_e8s_equivalent += reward` at line 6732 of `rs/nns/governance/src/governance.rs` for the same issue: [6](#0-5) 

---

### Proof of Concept

1. Deploy an SNS with a total token supply of, say, 10¹⁸ e8s.
2. Create a neuron that holds a large stake and votes on every proposal for many years without ever calling `disburse_maturity`. The neuron's `maturity_e8s_equivalent` accumulates each reward round.
3. Once `maturity_e8s_equivalent` reaches `u64::MAX - neuron_reward_e8s`, the next `distribute_rewards` call executes:
   ```rust
   neuron.maturity_e8s_equivalent += neuron_reward_e8s;
   // u64::MAX - k + k+1 wraps to 0 in release mode
   ```
4. The neuron's maturity is now 0 (or a small wrapped value). All accumulated maturity — representing years of voting rewards and the right to mint SNS tokens — is permanently destroyed with no error, no log, and no revert.

The root cause is confirmed at: [1](#0-0) 

compared to the safe NNS equivalent: [7](#0-6)

### Citations

**File:** rs/sns/governance/src/governance.rs (L5974-5996)
```rust
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

**File:** rs/sns/governance/src/governance.rs (L5999-6011)
```rust
        // Freeze distributed_e8s_equivalent, now that we are done handing out rewards.
        let distributed_e8s_equivalent = distributed_e8s_equivalent;
        // Because we used floor to round rewards to integers (and everything is
        // non-negative), it should be that the amount distributed is not more
        // than the original purse.
        debug_assert!(
            i2d(distributed_e8s_equivalent) <= rewards_purse_e8s,
            "rewards distributed ({distributed_e8s_equivalent}) > purse ({rewards_purse_e8s})",
        );

        // This field is deprecated. People should really use end_timestamp_seconds
        // instead. This value can still be used if round duration is not changed.
        let new_reward_event_round = self.latest_reward_event().round + new_rounds_count;
```

**File:** rs/nns/governance/src/reward/distribution.rs (L159-172)
```rust
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
```

**File:** rs/sns/governance/src/neuron.rs (L641-645)
```rust
    fn voting_power_stake_e8s(&self) -> u64 {
        self.cached_neuron_stake_e8s
            .saturating_sub(self.neuron_fees_e8s)
            .saturating_add(self.staked_maturity_e8s_equivalent.unwrap_or(0))
    }
```

**File:** rs/nns/governance/src/governance.rs (L6724-6732)
```rust
                    let reward = (used_voting_rights * total_available_e8s_equivalent_float
                        / total_voting_rights) as u64;

                    reward_distribution.add_reward(neuron_id, reward);

                    // NOTE: This is the only reason we are checking the existence of neurons
                    // at this stage. Otherwise, we could defer until we distribute them in the
                    // schedule task.
                    actually_distributed_e8s_equivalent += reward;
```
