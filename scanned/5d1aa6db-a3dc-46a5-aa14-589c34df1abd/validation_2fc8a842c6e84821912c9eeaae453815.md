### Title
Inconsistent Use of Checked Arithmetic for Neuron Maturity Accounting in SNS Governance Reward Distribution - (File: rs/sns/governance/src/governance.rs)

### Summary
The SNS governance canister's `distribute_rewards` function uses unchecked integer addition (`+` and `+=`) when crediting maturity to neurons during voting reward distribution. The analogous NNS governance reward distribution code uses `saturating_add` for the same operations. This inconsistency within the IC codebase is a direct analog to the original report's finding of inconsistent safe-transfer usage: some code paths use safe/checked operations while others do not.

### Finding Description
In `rs/sns/governance/src/governance.rs`, the `distribute_rewards` function credits voting rewards to neurons using bare `+` and `+=` operators:

```rust
// Line 5990-5994
if neuron.auto_stake_maturity.unwrap_or(false) {
    neuron.staked_maturity_e8s_equivalent = Some(
        neuron.staked_maturity_e8s_equivalent.unwrap_or(0) + neuron_reward_e8s,  // unchecked
    );
} else {
    neuron.maturity_e8s_equivalent += neuron_reward_e8s;  // unchecked
}
``` [1](#0-0) 

In contrast, the NNS governance canister's equivalent reward distribution code in `rs/nns/governance/src/reward/distribution.rs` uses `saturating_add` for both fields:

```rust
// Lines 163-171
neuron.staked_maturity_e8s_equivalent = Some(
    neuron.staked_maturity_e8s_equivalent.unwrap_or_default().saturating_add(reward_e8s),
);
// ...
neuron.maturity_e8s_equivalent = neuron.maturity_e8s_equivalent.saturating_add(reward_e8s);
``` [2](#0-1) 

The inconsistency is also visible **within the SNS governance canister itself**: `stake_maturity_of_neuron` in the same file uses `saturating_add` and `saturating_sub` for the same fields: [3](#0-2) 

### Impact Explanation
In Rust release builds (which production IC canisters use), integer overflow on primitive types wraps around silently rather than panicking. If `neuron.maturity_e8s_equivalent` or `staked_maturity_e8s_equivalent` is near `u64::MAX` and a reward is added, the value wraps to a small number, permanently destroying the neuron's accumulated maturity. Since maturity is the precursor to minting SNS tokens (via `disburse_maturity`), this constitutes a **ledger conservation bug**: tokens that should be mintable are silently lost. The `distributed_e8s_equivalent` counter on line 5996 also uses unchecked `+=`, meaning the reward event record could also be corrupted. [4](#0-3) 

### Likelihood Explanation
The likelihood of overflow is low in practice because `u64::MAX` (~1.8×10¹¹ tokens at 8 decimals) far exceeds any realistic SNS token supply. However, the inconsistency is a real code defect: the same logical operation is performed safely in NNS governance and in other SNS governance methods, but unsafely in `distribute_rewards`. The inconsistency itself is the finding — it indicates the safe path was not applied uniformly, matching the original report's pattern exactly. The entry path is fully unprivileged: any user who votes on SNS proposals causes their neuron to accumulate maturity through the periodic `distribute_rewards` timer task.

### Recommendation
Replace the unchecked `+` and `+=` operators in `distribute_rewards` with `saturating_add`, consistent with NNS governance's `continue_processing` and SNS governance's own `stake_maturity_of_neuron`:

```rust
// Staked maturity
neuron.staked_maturity_e8s_equivalent = Some(
    neuron.staked_maturity_e8s_equivalent.unwrap_or(0).saturating_add(neuron_reward_e8s),
);
// Unstaked maturity
neuron.maturity_e8s_equivalent = neuron.maturity_e8s_equivalent.saturating_add(neuron_reward_e8s);
// Counter
distributed_e8s_equivalent = distributed_e8s_equivalent.saturating_add(neuron_reward_e8s);
```

### Proof of Concept
1. Deploy an SNS with a token supply large enough that a single neuron accumulates maturity approaching `u64::MAX` over many reward rounds (or set `maturity_e8s_equivalent` directly in a test environment to `u64::MAX - 1`).
2. Trigger a reward round by advancing time past `round_duration_seconds` with a settled proposal.
3. `distribute_rewards` calls `neuron.maturity_e8s_equivalent += neuron_reward_e8s` at line 5994.
4. The addition wraps: `(u64::MAX - 1) + 2 = 1`, destroying the neuron's maturity.
5. The neuron owner calls `disburse_maturity` and receives 1 e8 instead of the expected large amount — tokens are permanently lost. [5](#0-4) [6](#0-5)

### Citations

**File:** rs/sns/governance/src/governance.rs (L1577-1586)
```rust
        neuron.maturity_e8s_equivalent = neuron
            .maturity_e8s_equivalent
            .saturating_sub(maturity_to_stake);

        neuron.staked_maturity_e8s_equivalent = Some(
            neuron
                .staked_maturity_e8s_equivalent
                .unwrap_or(0)
                .saturating_add(maturity_to_stake),
        );
```

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
