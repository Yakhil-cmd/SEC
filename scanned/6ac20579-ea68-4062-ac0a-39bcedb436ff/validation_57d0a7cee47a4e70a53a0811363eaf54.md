### Title
Unchecked Integer Overflow in SNS Governance Voting Reward Accumulation - (`rs/sns/governance/src/governance.rs`)

### Summary

The SNS governance `distribute_rewards` function uses plain Rust `+` and `+=` operators when crediting voting rewards to neuron maturity fields. In Rust release builds, integer overflow wraps silently. The NNS governance equivalent explicitly uses `saturating_add` for the same operation, confirming this is a known concern that was not applied consistently to SNS.

### Finding Description

In `rs/sns/governance/src/governance.rs`, the reward distribution loop applies per-neuron voting rewards using unchecked arithmetic: [1](#0-0) 

Specifically:
- `neuron.staked_maturity_e8s_equivalent.unwrap_or(0) + neuron_reward_e8s` — plain `+` on `u64`
- `neuron.maturity_e8s_equivalent += neuron_reward_e8s` — plain `+=` on `u64`
- `distributed_e8s_equivalent += neuron_reward_e8s` — plain `+=` on `u64`

In Rust, integer overflow in release builds wraps silently (two's complement). The `overflow-checks` profile flag is off by default in release mode.

The NNS governance equivalent, which performs the identical logical operation, explicitly uses `saturating_add`: [2](#0-1) 

This inconsistency is the root cause. The SNS code was not updated to match the defensive arithmetic used in NNS.

The `rewards_purse_e8s` is checked to fit in `u64` before distribution begins: [3](#0-2) 

However, individual neuron maturity fields (`maturity_e8s_equivalent`, `staked_maturity_e8s_equivalent`) are persistent state that accumulates across many reward rounds. There is no cap or overflow guard on these fields.

### Impact Explanation

If a neuron's `maturity_e8s_equivalent` or `staked_maturity_e8s_equivalent` wraps past `u64::MAX` (≈ 184 billion tokens in e8s), the field silently resets to a small value. This destroys the neuron's accumulated maturity — a ledger conservation violation. The neuron owner loses the ability to disburse or spawn that maturity. Additionally, `distributed_e8s_equivalent` wrapping would cause the emitted `RewardEvent` to record an incorrect (too-small) distributed amount, corrupting the governance audit trail.

### Likelihood Explanation

`u64::MAX` in e8s corresponds to approximately 184 billion governance tokens. For any single SNS, the total token supply is typically orders of magnitude smaller (commonly 1–10 billion tokens). A single neuron accumulating enough maturity to overflow `u64` would require holding a large fraction of the supply and earning rewards for an astronomically long time. Under current SNS economics this is practically unreachable. The likelihood is therefore **very low**, but the code is demonstrably incorrect relative to the NNS reference implementation, and the impact if triggered is severe (irreversible maturity destruction). The vulnerability is real but not immediately exploitable by an unprivileged actor under normal conditions.

### Recommendation

Replace the three plain arithmetic operations in the SNS reward distribution loop with overflow-safe equivalents, consistent with the NNS implementation:

```rust
// Instead of:
neuron.staked_maturity_e8s_equivalent.unwrap_or(0) + neuron_reward_e8s
// Use:
neuron.staked_maturity_e8s_equivalent.unwrap_or(0).saturating_add(neuron_reward_e8s)

// Instead of:
neuron.maturity_e8s_equivalent += neuron_reward_e8s;
// Use:
neuron.maturity_e8s_equivalent = neuron.maturity_e8s_equivalent.saturating_add(neuron_reward_e8s);

// Instead of:
distributed_e8s_equivalent += neuron_reward_e8s;
// Use:
distributed_e8s_equivalent = distributed_e8s_equivalent.saturating_add(neuron_reward_e8s);
```

### Proof of Concept

The divergence between SNS and NNS is directly observable by comparing the two reward distribution sites:

**SNS (vulnerable)** — `rs/sns/governance/src/governance.rs` lines 5989–5996:
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

**NNS (safe)** — `rs/nns/governance/src/reward/distribution.rs` lines 163–171:
```rust
neuron.staked_maturity_e8s_equivalent = Some(
    neuron.staked_maturity_e8s_equivalent
        .unwrap_or_default()
        .saturating_add(reward_e8s),
);
// ...
neuron.maturity_e8s_equivalent =
    neuron.maturity_e8s_equivalent.saturating_add(reward_e8s);
``` [1](#0-0) [4](#0-3)

### Citations

**File:** rs/sns/governance/src/governance.rs (L5878-5889)
```rust
        let total_available_e8s_equivalent = Some(match u64::try_from(rewards_purse_e8s) {
            Ok(ok) => ok,
            Err(err) => {
                log!(
                    ERROR,
                    "Looks like the rewards purse ({}) overflowed u64: {}. \
                     Therefore, we stop the current attempt to distribute voting rewards.",
                    rewards_purse_e8s,
                    err,
                );
                return;
            }
```

**File:** rs/sns/governance/src/governance.rs (L5989-5996)
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
