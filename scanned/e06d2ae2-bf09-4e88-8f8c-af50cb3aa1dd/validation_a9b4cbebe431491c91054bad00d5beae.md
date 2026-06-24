### Title
Unchecked Arithmetic Overflow in SNS Governance Reward Distribution Causes Silent Loss of Neuron Maturity - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

In `rs/sns/governance/src/governance.rs`, the SNS voting reward distribution loop uses bare `+` and `+=` operators on `u64` fields when crediting `neuron_reward_e8s` to neuron maturity. In Rust release builds, integer overflow wraps silently (two's complement). If a neuron's `maturity_e8s_equivalent` or `staked_maturity_e8s_equivalent` is near `u64::MAX` when a reward round fires, the addition wraps to a small value, permanently destroying the neuron holder's accumulated maturity. The NNS governance implementation of the same operation uses `saturating_add`, making the inconsistency clear.

---

### Finding Description

In the SNS governance reward distribution path, after computing each neuron's share of the rewards purse, the code writes:

```rust
if neuron.auto_stake_maturity.unwrap_or(false) {
    neuron.staked_maturity_e8s_equivalent = Some(
        neuron.staked_maturity_e8s_equivalent.unwrap_or(0) + neuron_reward_e8s,
    );
} else {
    neuron.maturity_e8s_equivalent += neuron_reward_e8s;
}
distributed_e8s_equivalent += neuron_reward_e8s;
```

All three additions are unchecked. In Rust, `u64 + u64` and `u64 += u64` wrap on overflow in release builds (no panic, no error). The NNS governance performs the identical operation with `saturating_add`:

```rust
neuron.maturity_e8s_equivalent =
    neuron.maturity_e8s_equivalent.saturating_add(reward_e8s);
```

The structural parallel to the external report is exact:
- **External report**: `feeGrowthGlobalAsset` accumulator overflows and laps the user's checkpoint → distance becomes zero → user earns 0 fees.
- **IC analog**: `maturity_e8s_equivalent` accumulator overflows on reward credit → value wraps to near-zero → neuron holder loses all accumulated maturity.

Additionally, `distributed_e8s_equivalent += neuron_reward_e8s` overflowing would corrupt the `RewardEvent` record, causing `e8s_equivalent_to_be_rolled_over()` to return a wrong value and poisoning all future reward-purse calculations for that SNS.

---

### Impact Explanation

A neuron whose `maturity_e8s_equivalent` is near `u64::MAX` (≈ 1.84 × 10¹⁹) receives a reward credit. The addition wraps to a small value. The neuron holder's entire accumulated maturity is silently destroyed. Because the write is committed to stable state before any error can be detected, the loss is permanent. For SNS tokens with large total supplies or small denomination units (e.g., 18-decimal tokens), the threshold is reachable with far fewer tokens than for ICP. The `distributed_e8s_equivalent` overflow is a secondary impact that corrupts future reward accounting for the entire SNS.

---

### Likelihood Explanation

The reward distribution heartbeat fires automatically every round without any privileged action. No attacker input is required; the overflow is triggered purely by the passage of time and accumulation of maturity. For SNS tokens with large supplies or small denomination units the threshold is reachable. Even for typical SNS tokens, a long-lived neuron with a large stake in a high-reward-rate SNS can accumulate maturity over years. The inconsistency with the NNS implementation (which uses `saturating_add`) suggests this was an oversight rather than a deliberate design choice.

---

### Recommendation

Replace all three unchecked additions with `saturating_add` (matching the NNS governance pattern) or `checked_add` with an explicit error log and early return:

```rust
if neuron.auto_stake_maturity.unwrap_or(false) {
    neuron.staked_maturity_e8s_equivalent = Some(
        neuron.staked_maturity_e8s_equivalent
            .unwrap_or(0)
            .saturating_add(neuron_reward_e8s),
    );
} else {
    neuron.maturity_e8s_equivalent =
        neuron.maturity_e8s_equivalent.saturating_add(neuron_reward_e8s);
}
distributed_e8s_equivalent =
    distributed_e8s_equivalent.saturating_add(neuron_reward_e8s);
```

---

### Proof of Concept

1. Deploy an SNS with a token whose total supply is large enough that a single neuron can accumulate maturity approaching `u64::MAX` (e.g., a token with 18 decimal places and a supply of 10^10 tokens, where `u64::MAX` in e8s-equivalent is only ~18 tokens).
2. Create a neuron with a large stake and enable `auto_stake_maturity = false`.
3. Allow the neuron to accumulate `maturity_e8s_equivalent` to `u64::MAX - K` for a small `K`.
4. Wait for the next reward round to fire. The governance heartbeat calls `distribute_rewards`, which computes `neuron_reward_e8s > K` and executes `neuron.maturity_e8s_equivalent += neuron_reward_e8s`.
5. In a release build, the result wraps to `(u64::MAX - K + neuron_reward_e8s) % 2^64`, a value near zero.
6. The neuron holder's entire accumulated maturity is permanently lost with no error logged. [1](#0-0) [2](#0-1)

### Citations

**File:** rs/sns/governance/src/governance.rs (L5987-5996)
```rust
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
