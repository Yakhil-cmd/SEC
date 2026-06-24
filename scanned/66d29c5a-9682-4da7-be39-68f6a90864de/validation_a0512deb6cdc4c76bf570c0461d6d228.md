### Title
Unchecked Arithmetic in SNS Governance Reward Distribution Silently Corrupts Neuron Maturity - (File: rs/sns/governance/src/governance.rs)

### Summary
The SNS governance reward distribution function uses plain Rust `+` and `+=` operators (which wrap silently in release builds) when accumulating maturity rewards into neuron fields, while the analogous NNS governance code correctly uses `saturating_add`. An overflow would silently corrupt a neuron's maturity balance, destroying accumulated rewards without any error signal.

### Finding Description
In `rs/sns/governance/src/governance.rs`, the function that distributes voting rewards to neurons uses unchecked arithmetic at three points:

```rust
// Line 5991 - plain + on u64
neuron.staked_maturity_e8s_equivalent = Some(
    neuron.staked_maturity_e8s_equivalent.unwrap_or(0) + neuron_reward_e8s,
);
// Line 5994 - plain += on u64
neuron.maturity_e8s_equivalent += neuron_reward_e8s;
// Line 5996 - plain += on u64
distributed_e8s_equivalent += neuron_reward_e8s;
``` [1](#0-0) 

In contrast, the NNS governance equivalent in `rs/nns/governance/src/reward/distribution.rs` uses `saturating_add` for the same maturity accumulation:

```rust
neuron.staked_maturity_e8s_equivalent = Some(
    neuron.staked_maturity_e8s_equivalent.unwrap_or_default().saturating_add(reward_e8s),
);
neuron.maturity_e8s_equivalent = neuron.maturity_e8s_equivalent.saturating_add(reward_e8s);
``` [2](#0-1) 

IC canisters are compiled in release mode for production. In Rust release builds, integer overflow on primitive types wraps silently (no panic, no trap). The `maturity_e8s_equivalent` and `staked_maturity_e8s_equivalent` fields are both `u64`.

A secondary instance exists in NNS governance at the `actually_distributed_e8s_equivalent += reward` accumulator: [3](#0-2) 

### Impact Explanation
If a neuron's `maturity_e8s_equivalent` or `staked_maturity_e8s_equivalent` reaches near `u64::MAX` (~184 billion tokens in e8s) and a reward is added, the value wraps to a small number in release mode. This silently destroys the neuron's accumulated maturity — the neuron controller loses their earned rewards with no error returned and no on-chain evidence of the corruption. The `distributed_e8s_equivalent` counter also wrapping would cause the governance canister to misreport total distributed rewards, breaking accounting invariants.

### Likelihood Explanation
The likelihood is low but non-zero. `u64::MAX` in e8s is ~184 billion tokens. For most SNS deployments with smaller total supplies, a single neuron accumulating this much maturity is impractical. However, for SNS tokens with large supplies and neurons that never disburse maturity over many years of compounding rewards, the ceiling could theoretically be approached. The risk is elevated by the fact that the NNS already identified this pattern as requiring `saturating_add`, yet the SNS code was not updated consistently.

### Recommendation
Replace the plain `+` and `+=` operators with `saturating_add` (consistent with the NNS implementation) or `checked_add` with an explicit error/log path:

```rust
// Line 5991
neuron.staked_maturity_e8s_equivalent = Some(
    neuron.staked_maturity_e8s_equivalent.unwrap_or(0)
        .saturating_add(neuron_reward_e8s),
);
// Line 5994
neuron.maturity_e8s_equivalent =
    neuron.maturity_e8s_equivalent.saturating_add(neuron_reward_e8s);
// Line 5996
distributed_e8s_equivalent =
    distributed_e8s_equivalent.saturating_add(neuron_reward_e8s);
```

### Proof of Concept

1. Deploy an SNS with a large total token supply.
2. Create a neuron with a large stake and enable `auto_stake_maturity`.
3. Allow the neuron to accumulate rewards over many reward periods without ever calling `disburse_maturity` or `stake_maturity`.
4. When `maturity_e8s_equivalent` or `staked_maturity_e8s_equivalent` approaches `u64::MAX`, the next call to the internal reward distribution timer (triggered automatically by the governance canister) executes:
   ```rust
   neuron.maturity_e8s_equivalent += neuron_reward_e8s;
   ```
   In a release build this wraps to a small value. The neuron controller's maturity is silently zeroed out. No error is returned to any caller; the governance canister continues operating normally with corrupted state. [4](#0-3) [5](#0-4)

### Citations

**File:** rs/sns/governance/src/governance.rs (L5954-5997)
```rust
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

**File:** rs/nns/governance/src/governance.rs (L6732-6732)
```rust
                    actually_distributed_e8s_equivalent += reward;
```
