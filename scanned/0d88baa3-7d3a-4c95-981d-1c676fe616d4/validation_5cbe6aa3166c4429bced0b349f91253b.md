### Title
Unchecked Integer Overflow in `disburse_maturity` Causes Canister Trap for Neuron Owners with Large Maturity — (File: rs/sns/governance/src/governance.rs)

---

### Summary

The `disburse_maturity` function in SNS governance uses `.expect()` on a `checked_mul` result when computing the amount of maturity to deduct. The function validates only that `percentage_to_disburse` is in `[1, 100]`, but imposes no documented upper bound on `maturity_e8s_equivalent`. If `maturity_e8s_equivalent > u64::MAX / 100 ≈ 1.84 × 10¹⁷ e8s`, the multiplication overflows `u64`, `checked_mul` returns `None`, and `.expect()` panics — trapping the canister and permanently blocking that neuron owner from disbursing their maturity.

---

### Finding Description

In `rs/sns/governance/src/governance.rs`, the `disburse_maturity` function performs the following computation:

```rust
let maturity_to_deduct = neuron
    .maturity_e8s_equivalent
    .checked_mul(disburse_maturity.percentage_to_disburse as u64)
    .expect("Overflow while processing maturity to disburse.")
    .checked_div(100)
    .expect("Error when processing maturity to disburse.")
    as u128;
``` [1](#0-0) 

The function validates that `percentage_to_disburse` is between 1 and 100: [2](#0-1) 

However, no constraint is placed on `maturity_e8s_equivalent`. The multiplication `maturity_e8s_equivalent * percentage_to_disburse` is performed in `u64`. When `maturity_e8s_equivalent > u64::MAX / 100 ≈ 1.84 × 10¹⁷ e8s`, `checked_mul` returns `None` and the `.expect()` call panics, trapping the canister execution.

This is structurally identical to the Cobb-Douglas analog: the function documents one set of input constraints (`percentage_to_disburse ∈ [1, 100]`) but silently requires an additional undocumented constraint (`maturity_e8s_equivalent < u64::MAX / percentage_to_disburse`) to avoid a fatal arithmetic failure.

The same pattern exists in NNS governance `spawn_neuron`: [3](#0-2) 

However, for NNS the total ICP supply (~5 × 10¹⁶ e8s) is below the overflow threshold, making it unreachable there. For SNS tokens the supply is unconstrained and the threshold is reachable.

The SNS governance reward distribution accumulates maturity using unchecked integer addition: [4](#0-3) 

In Rust release mode (production IC canisters), `+=` wraps silently on overflow. This means `maturity_e8s_equivalent` can grow past `u64::MAX / 100` without any error being raised during reward distribution, setting up the later trap in `disburse_maturity`.

---

### Impact Explanation

When a neuron owner calls `manage_neuron` → `DisburseMaturity` and their `maturity_e8s_equivalent` exceeds `u64::MAX / 100`, the SNS governance canister traps. The message execution is rolled back, but the neuron's maturity remains locked at the large value. Every subsequent `disburse_maturity` call by that neuron owner will also trap. The neuron owner is permanently unable to convert their accumulated maturity into SNS tokens through the normal disbursement path, effectively freezing their earned rewards. This matches the external report's impact: "blocking essential operations."

---

### Likelihood Explanation

An SNS with:
- A large token supply (e.g., 10¹⁸ e8s = 10¹⁰ tokens),
- A high initial reward rate (e.g., 10–100% per year), and
- A single neuron holding a large fraction of the stake

can accumulate `maturity_e8s_equivalent > 1.84 × 10¹⁷ e8s` within 1–20 years of operation. Because the reward distribution loop uses unchecked `+=`, the maturity grows monotonically in release mode until it wraps at `u64::MAX` (which takes far longer than reaching the overflow threshold). The window during which `disburse_maturity` traps is therefore the entire period from when maturity first exceeds `u64::MAX / 100` until it wraps — potentially spanning years. Any neuron owner whose maturity enters this range is affected.

---

### Recommendation

Replace the `.expect()` calls with proper error propagation:

```rust
let maturity_to_deduct = neuron
    .maturity_e8s_equivalent
    .checked_mul(disburse_maturity.percentage_to_disburse as u64)
    .and_then(|v| v.checked_div(100))
    .ok_or_else(|| GovernanceError::new_with_message(
        ErrorType::PreconditionFailed,
        "Arithmetic overflow computing maturity to disburse.",
    ))?;
```

Additionally, the reward distribution loop should use `saturating_add` or `checked_add` with error logging instead of bare `+=` to prevent silent maturity wrap-around: [5](#0-4) 

Document the effective upper bound on `maturity_e8s_equivalent` for safe operation of `disburse_maturity`.

---

### Proof of Concept

1. Deploy an SNS with a large token supply (e.g., 10¹⁰ tokens = 10¹⁸ e8s) and a high reward rate.
2. Create a neuron holding a large fraction of the stake and vote on all proposals for an extended period until `maturity_e8s_equivalent > u64::MAX / 100 ≈ 1.84 × 10¹⁷ e8s`.
3. Call `manage_neuron` with `DisburseMaturity { percentage_to_disburse: 100, to_account: ... }`.
4. The canister traps at line 1646 with `"Overflow while processing maturity to disburse."`, the call fails, and the neuron owner cannot disburse their maturity regardless of how many times they retry.

Minimal triggering state: `maturity_e8s_equivalent = u64::MAX / 100 + 1 = 184_467_440_737_095_517`, `percentage_to_disburse = 100`. The multiplication `184_467_440_737_095_517 * 100 = 18_446_744_073_709_551_700 > u64::MAX`, so `checked_mul` returns `None` and `.expect()` panics.

### Citations

**File:** rs/sns/governance/src/governance.rs (L1633-1640)
```rust
        if disburse_maturity.percentage_to_disburse > 100
            || disburse_maturity.percentage_to_disburse == 0
        {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "The percentage of maturity to disburse must be a value between 1 and 100 (inclusive).",
            ));
        }
```

**File:** rs/sns/governance/src/governance.rs (L1643-1649)
```rust
        let maturity_to_deduct = neuron
            .maturity_e8s_equivalent
            .checked_mul(disburse_maturity.percentage_to_disburse as u64)
            .expect("Overflow while processing maturity to disburse.")
            .checked_div(100)
            .expect("Error when processing maturity to disburse.")
            as u128;
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

**File:** rs/nns/governance/src/governance.rs (L2643-2647)
```rust
        let maturity_to_spawn = parent_neuron
            .maturity_e8s_equivalent
            .checked_mul(percentage as u64)
            .expect("Overflow while processing maturity to spawn.");
        let maturity_to_spawn = maturity_to_spawn.checked_div(100).unwrap();
```
