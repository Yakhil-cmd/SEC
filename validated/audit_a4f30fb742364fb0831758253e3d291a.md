### Title
Unchecked Arithmetic in SNS Governance `distribute_rewards` Causes Silent Maturity Wrap-Around - (File: rs/sns/governance/src/governance.rs)

### Summary
The SNS governance `distribute_rewards` function uses plain Rust `+` and `+=` operators on `u64` fields for neuron maturity accumulation and reward tracking. In Rust release builds (used for IC wasm canisters), unsigned integer overflow silently wraps modulo 2^64 rather than panicking. The analogous NNS governance reward distribution code correctly uses `saturating_add`. This inconsistency means that in an SNS with a sufficiently large token supply, a neuron's accumulated maturity or the `distributed_e8s_equivalent` tracking variable can silently wrap to a near-zero value, causing permanent, undetected loss of maturity entitlements and corrupted reward-rollover accounting.

### Finding Description

In `rs/sns/governance/src/governance.rs`, the `distribute_rewards` function accumulates per-neuron voting rewards using bare arithmetic: [1](#0-0) 

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

Three separate `u64` additions are unchecked:
1. `staked_maturity_e8s_equivalent.unwrap_or(0) + neuron_reward_e8s`
2. `maturity_e8s_equivalent += neuron_reward_e8s`
3. `distributed_e8s_equivalent += neuron_reward_e8s`

The NNS governance equivalent in `rs/nns/governance/src/reward/distribution.rs` uses `saturating_add` for both fields: [2](#0-1) 

```rust
neuron.staked_maturity_e8s_equivalent = Some(
    neuron.staked_maturity_e8s_equivalent.unwrap_or_default()
        .saturating_add(reward_e8s),
);
// ...
neuron.maturity_e8s_equivalent =
    neuron.maturity_e8s_equivalent.saturating_add(reward_e8s);
```

In Rust release builds targeting wasm32 (the IC canister compilation target), unsigned integer overflow wraps silently. There is no trap. The `distributed_e8s_equivalent` variable is subsequently used to compute the `rolled_over_e8s` for the next reward event: [3](#0-2) 

If `distributed_e8s_equivalent` wraps, the rolled-over amount is computed incorrectly, causing the reward purse for subsequent rounds to be inflated beyond the intended supply cap.

### Impact Explanation

**Ledger conservation bug / governance accounting bug.** Two distinct impacts:

1. **Neuron maturity destruction**: If a neuron's `maturity_e8s_equivalent` or `staked_maturity_e8s_equivalent` wraps, the neuron permanently loses its accumulated maturity. Maturity represents a future ICP/SNS-token minting entitlement; its loss is a direct financial loss to the neuron holder with no recovery path.

2. **Reward rollover inflation**: If `distributed_e8s_equivalent` wraps, the computed `rolled_over_e8s` for the next reward event is wrong (too large), causing the SNS to mint more tokens than the governance parameters intend in subsequent rounds, violating the token supply invariant.

### Likelihood Explanation

**Low but non-zero.** The overflow threshold is `u64::MAX ≈ 1.84 × 10^19 e8s ≈ 184 billion tokens`. An SNS with a large initial supply (e.g., 10^12 tokens = 10^20 e8s) and a single neuron holding a large fraction of voting power could accumulate maturity exceeding `u64::MAX` over many reward rounds if the reward rate is high. The `distributed_e8s_equivalent` accumulates across *all* neurons in a single round, making it more susceptible: if the total rewards distributed in one round across all neurons exceeds `u64::MAX`, the tracking variable wraps. This is reachable by an unprivileged SNS participant who simply holds a neuron and votes; no privileged access is required. The bug would have been caught by property-based or fuzz testing of the reward distribution path — precisely the gap identified in the external report.

### Recommendation

Replace all three bare arithmetic operations in `distribute_rewards` with `saturating_add` (matching the NNS governance implementation) or `checked_add` with an explicit error log and early return:

```rust
// staked maturity
neuron.staked_maturity_e8s_equivalent = Some(
    neuron.staked_maturity_e8s_equivalent.unwrap_or(0)
        .saturating_add(neuron_reward_e8s),
);
// unstaked maturity
neuron.maturity_e8s_equivalent =
    neuron.maturity_e8s_equivalent.saturating_add(neuron_reward_e8s);
// tracking variable
distributed_e8s_equivalent =
    distributed_e8s_equivalent.saturating_add(neuron_reward_e8s);
```

Add a property-based test asserting that `distribute_rewards` never decreases any neuron's maturity fields and that `distributed_e8s_equivalent` never exceeds `rewards_purse_e8s`.

### Proof of Concept

1. Deploy an SNS with `total_token_supply = 2 × 10^11` tokens (2 × 10^19 e8s, just above `u64::MAX`).
2. Create a single neuron holding the entire voting power with `auto_stake_maturity = true`.
3. Run reward distribution rounds until `staked_maturity_e8s_equivalent` approaches `u64::MAX`.
4. On the next `distribute_rewards` call, `neuron.staked_maturity_e8s_equivalent.unwrap_or(0) + neuron_reward_e8s` wraps to a value near zero.
5. Observe that the neuron's `staked_maturity_e8s_equivalent` is now near zero despite having accumulated rewards for many rounds — permanent, silent loss with no on-chain error or event emitted.

The root cause is exclusively in the SNS governance production code at: [1](#0-0) 
and is absent from the NNS governance equivalent at: [2](#0-1)

### Citations

**File:** rs/sns/governance/src/governance.rs (L5942-5946)
```rust
        let mut distributed_e8s_equivalent = 0_u64;
        // Now that we know the size of the pie (rewards_purse_e8s), and how
        // much of it each neuron is supposed to get (*_reward_shares), we now
        // proceed to actually handing out those rewards.
        if total_reward_shares == dec!(0) {
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

**File:** rs/nns/governance/src/reward/distribution.rs (L163-172)
```rust
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
