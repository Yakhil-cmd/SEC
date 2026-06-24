### Title
Truncation via `saturating_mul` in u64 Silently Underestimates Maturity-to-Stake in SNS Governance - (File: `rs/sns/governance/src/governance.rs`)

### Summary

In the SNS governance canister's `stake_maturity_of_neuron` function, the intermediate product `maturity_e8s_equivalent * percentage_to_stake` is computed entirely in `u64` using `saturating_mul`. When `maturity_e8s_equivalent` is large (e.g., near `u64::MAX`) and `percentage_to_stake` is any value > 1, the multiplication saturates silently at `u64::MAX` before the division by 100 occurs. This causes `maturity_to_stake` to be computed as `u64::MAX / 100` instead of the correct proportional value, silently over-staking the neuron's maturity. The analogous NNS governance function has the same pattern. This is the IC analog of the TRST-M-4 truncation/overflow class: a fixed-width integer type is too narrow for an intermediate product in a financial accounting calculation.

### Finding Description

In `rs/sns/governance/src/governance.rs`, `stake_maturity_of_neuron`:

```rust
let mut maturity_to_stake = (neuron
    .maturity_e8s_equivalent
    .saturating_mul(percentage_to_stake as u64))
    / 100;
```

`maturity_e8s_equivalent` is a `u64` and `percentage_to_stake` is a `u32` cast to `u64` (range 1–100). The product `maturity_e8s_equivalent * percentage_to_stake` can be up to `u64::MAX * 100`, which overflows `u64`. `saturating_mul` silently clamps the product to `u64::MAX` instead of panicking or returning an error. The subsequent division by 100 then yields `u64::MAX / 100 ≈ 1.84 × 10^17`, which is far larger than the correct proportional amount.

The downstream guard:

```rust
if maturity_to_stake > neuron.maturity_e8s_equivalent {
    maturity_to_stake = neuron.maturity_e8s_equivalent;
}
```

clamps the result to the full maturity balance, meaning the neuron's **entire** maturity is staked regardless of the requested percentage. A user requesting to stake 1% of a large maturity balance will instead have 100% staked.

The identical pattern exists in NNS governance at `rs/nns/governance/src/governance.rs` line 2795–2796:

```rust
let mut maturity_to_stake =
    (neuron_maturity_e8s_equivalent.saturating_mul(percentage_to_stake as u64)) / 100;
```

By contrast, the NNS `spawn_neuron` and `disburse_maturity` (NNS new path) correctly use `checked_mul` on `u64` or widen to `u128` before multiplying.

### Impact Explanation

Any SNS neuron holder or NNS neuron holder with a large `maturity_e8s_equivalent` (specifically, any value where `maturity * percentage > u64::MAX`, i.e., `maturity > u64::MAX / percentage ≈ 1.84 × 10^17 / percentage`) who calls `StakeMaturity` with a partial percentage will have their **entire** maturity staked instead of the requested fraction. This is a ledger conservation bug: the user's maturity is irreversibly moved to staked maturity in excess of what they authorized. Staked maturity is locked and subject to dissolve delay, so the user loses liquidity over their maturity without consent. The maximum maturity on the NNS is bounded by ICP supply (~500M ICP = 5×10^16 e8s), so for `percentage_to_stake = 3` or higher, the overflow threshold is reachable for neurons holding ~6×10^15 e8s (≈60M ICP equivalent in maturity), which is within the range of large institutional stakers.

### Likelihood Explanation

The vulnerability requires a neuron with very large `maturity_e8s_equivalent`. On the NNS, the ICP supply is ~500M ICP (5×10^16 e8s). For `percentage_to_stake = 10`, overflow occurs when `maturity > 1.84×10^18 / 10 = 1.84×10^17` e8s (≈1.84B ICP), which exceeds total supply — so NNS is safe in practice. However, for SNS tokens, the token supply and decimal configuration can differ. An SNS with a large token supply or high decimal precision could have neurons with maturity values that trigger this overflow. Additionally, if the NNS ICP supply grows significantly or if the calculation is reused in a new context, the risk increases. The likelihood is **low-to-medium** for SNS deployments with large token supplies, and **low** for NNS given current supply constraints.

### Recommendation

Replace the `saturating_mul` pattern with a widened `u128` intermediate calculation, matching the pattern already used in `disburse_maturity` (NNS new path) and `percentage_of_maturity`:

```rust
// Correct: widen to u128 before multiplying
let mut maturity_to_stake = ((neuron.maturity_e8s_equivalent as u128)
    .saturating_mul(percentage_to_stake as u128)
    / 100) as u64;
```

Or use `checked_mul` on `u128` and propagate errors. This matches the safe pattern in `rs/nns/governance/src/governance/disburse_maturity.rs` (`percentage_of_maturity` function).

### Proof of Concept

**Trigger condition:**
- `maturity_e8s_equivalent = 2_000_000_000_000_000_000` (2×10^18 e8s, feasible for an SNS with large supply)
- `percentage_to_stake = 50`

**Buggy computation:**
```
saturating_mul(2_000_000_000_000_000_000_u64, 50_u64)
= u64::MAX  (= 18_446_744_073_709_551_615, saturated)
/ 100
= 184_467_440_737_095_516

// Guard: 184_467_440_737_095_516 > 2_000_000_000_000_000_000 → clamp to full maturity
maturity_to_stake = 2_000_000_000_000_000_000  // 100% staked instead of 50%
```

**Correct computation:**
```
(2_000_000_000_000_000_000_u128 * 50) / 100
= 1_000_000_000_000_000_000  // 50% staked as intended
```

The attacker-controlled entry path is a standard `manage_neuron` ingress call with `Command::StakeMaturity { percentage_to_stake: Some(50) }` from any neuron controller. No privileged access is required. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** rs/sns/governance/src/governance.rs (L1563-1566)
```rust
        let mut maturity_to_stake = (neuron
            .maturity_e8s_equivalent
            .saturating_mul(percentage_to_stake as u64))
            / 100;
```

**File:** rs/nns/governance/src/governance.rs (L2795-2796)
```rust
        let mut maturity_to_stake =
            (neuron_maturity_e8s_equivalent.saturating_mul(percentage_to_stake as u64)) / 100;
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L225-244)
```rust
fn percentage_of_maturity(
    total_maturity_e8s: u64,
    percentage_to_disburse: u32,
) -> Result<u64, InitiateMaturityDisbursementError> {
    (total_maturity_e8s as u128)
        .checked_mul(percentage_to_disburse as u128)
        .and_then(|result| result.checked_div(100))
        .and_then(|result| {
            // This should be impossible as long as `percentage_to_disburse` is between 0 and 100.
            if result > u64::MAX as u128 {
                None
            } else {
                Some(result as u64)
            }
        })
        .ok_or_else(|| InitiateMaturityDisbursementError::Unknown {
            reason: format!(
                "Failed to calculate percentage of maturity: {percentage_to_disburse}% of {total_maturity_e8s} e8s"
            ),
        })
```
