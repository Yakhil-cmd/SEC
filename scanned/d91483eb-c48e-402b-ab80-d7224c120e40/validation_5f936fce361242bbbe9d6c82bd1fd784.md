### Title
Integer Division Truncation in `compute_maturity_modulation_permyriad` Creates Dead Zone in Maturity Modulation Formula - (File: rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs)

---

### Summary

The function `compute_maturity_modulation_permyriad` computes the target maturity modulation using integer division. When the ICP price deviation between the 7-day and 365-day averages is small — specifically when `sensitivity * |recent - reference| < reference` — the result truncates to 0, creating a dead zone where small price movements produce no modulation signal. This causes the on-chain maturity modulation to diverge from the intended formula, analogous to the `snapAccumulator` rounding issue in Roller.sol.

---

### Finding Description

In `compute_maturity_modulation_permyriad`, the target modulation is computed at line 157 as:

```rust
let target_modulation = {
    let recent = recent_icp_price as i128;
    let reference = reference_icp_price as i128;
    let sensitivity = MATURITY_MODULATION_SENSITIVITY_PERMYRIAD as i128; // 2_500
    sensitivity * (recent - reference) / reference
};
``` [1](#0-0) 

This is integer (floor-toward-zero) division. The condition for truncation to zero is:

```
|2500 * (recent - reference)| < reference
⟺ |recent - reference| < reference / 2500
```

For a typical reference price of `50,000` xdr_permyriad_per_icp (≈ 5 XDR/ICP), the dead zone is any deviation smaller than `50,000 / 2500 = 20` xdr_permyriad_per_icp (≈ 0.002 XDR/ICP, or 0.04% of the reference price). Within this range, the formula always returns 0 regardless of the true fractional permyriad value.

The constants involved: [2](#0-1) 

The `compute_average_icp_xdr_rate` helper also uses integer division for the average:

```rust
Some((sum / count as u128) as u64)
``` [3](#0-2) 

This introduces an additional rounding error of up to 1 xdr_permyriad_per_icp in both `recent_icp_price` and `reference_icp_price` before they reach the modulation formula, compounding the dead zone effect.

---

### Impact Explanation

The maturity modulation value (in permyriad) is stored in `MaturityModulation::current_value_permyriad` and is applied when NNS neurons disburse maturity, directly scaling the ICP minted: [4](#0-3) 

The application formula in `apply_maturity_modulation` multiplies the maturity amount by `(10_000 + modulation) / 10_000`: [5](#0-4) 

When the target modulation is incorrectly rounded to 0 instead of ±1 permyriad, the ICP minted per maturity disbursement is off by up to 0.01% (1 permyriad). For a neuron with 1,000 ICP of maturity, this is a 0.1 ICP error per disbursement. The error is bounded by the global clamp `[-1000, 200]` permyriad and the daily speed limit of 30 permyriad: [6](#0-5) 

The modulation diverges from the whitepaper formula whenever the 7-day and 365-day ICP price averages are close but not equal — a common real-world condition during periods of price stability.

---

### Likelihood Explanation

The dead zone is triggered whenever the absolute price deviation between the 7-day and 365-day moving averages is less than `reference / 2500`. For any reference price above ~1 XDR/ICP, this dead zone spans at least 4 xdr_permyriad_per_icp. During periods of price stability (which are common), the 7-day average frequently sits within this band of the 365-day average. The probability of this condition occurring on any given day is high, matching the original report's characterization of "high probability, low impact."

---

### Recommendation

Reorder the division to avoid premature truncation by multiplying before dividing, or use a fixed-point intermediate with sufficient precision:

```rust
// Instead of: sensitivity * (recent - reference) / reference
// Use: (sensitivity * (recent - reference) + reference / 2) / reference
// (adding reference/2 rounds to nearest instead of truncating)
let target_modulation = {
    let numerator = sensitivity * (recent - reference);
    let half_reference = reference / 2;
    if numerator >= 0 {
        (numerator + half_reference) / reference
    } else {
        (numerator - half_reference) / reference
    }
};
```

Alternatively, confirm that rounding down by up to 1 permyriad per daily update is acceptable given the speed limit and global bounds, and document this as a known approximation.

---

### Proof of Concept

Concrete numeric example demonstrating the dead zone:

- `reference_icp_price = 50_000` (5 XDR/ICP, a plausible long-term average)
- `recent_icp_price = 50_019` (price rose by 0.038%, within the dead zone)
- True target: `2500 * (50_019 - 50_000) / 50_000 = 2500 * 19 / 50_000 = 47_500 / 50_000 = 0` (integer division)
- Mathematical target: `2500 × 19 / 50_000 = 0.95` permyriad → should round to 1 permyriad
- Result: modulation target is 0 instead of 1 permyriad; the formula diverges from the intended calculation

For the negative direction:
- `recent_icp_price = 49_981` (price fell by 0.038%)
- `2500 * (49_981 - 50_000) / 50_000 = 2500 * (-19) / 50_000 = -47_500 / 50_000 = 0` (Rust truncates toward zero)
- Mathematical target: `-0.95` permyriad → should be -1 permyriad
- Result: modulation target is 0 instead of -1 permyriad

The dead zone spans `[-19, +19]` xdr_permyriad_per_icp around the reference price (for reference = 50,000), meaning any day where the 7-day average is within 0.038% of the 365-day average produces a zero target modulation regardless of the true fractional value. [1](#0-0)

### Citations

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L38-50)
```rust
/// How much the relative difference between current and reference ICP price affects maturity
/// modulation. k = 0.25 means a 10% price increase yields a 2.5% modulation boost.
/// Expressed in permyriad: 0.25 * 10_000 = 2_500.
const MATURITY_MODULATION_SENSITIVITY_PERMYRIAD: i64 = 2_500;

/// Maximum daily change in maturity modulation: 0.3% = 30 permyriad.
const MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD: i64 = 30;

/// Lower bound for Mission 70 maturity modulation: -10% = -1000 permyriad.
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000;

/// Upper bound for Mission 70 maturity modulation: +2% = 200 permyriad.
pub(crate) const MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70: i64 = 200;
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L110-113)
```rust
    if count == 0 {
        return None;
    }
    Some((sum / count as u128) as u64)
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L153-158)
```rust
    let target_modulation = {
        let recent = recent_icp_price as i128;
        let reference = reference_icp_price as i128;
        let sensitivity = MATURITY_MODULATION_SENSITIVITY_PERMYRIAD as i128;
        sensitivity * (recent - reference) / reference
    };
```

**File:** rs/nns/governance/src/governance.rs (L6427-6447)
```rust
        let maturity_modulation = match self
            .heap_data
            .maturity_modulation
            .as_ref()
            .and_then(|m| m.current_value_permyriad)
        {
            None => return,
            Some(value) => value,
        };

        // Sanity check that the maturity modulation returned is within bounds.
        if !VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE.contains(&maturity_modulation) {
            println!(
                "{}Maturity modulation (in basis points) out-of-bounds. Should be in range [{}, {}], actually is: {}",
                LOG_PREFIX,
                MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70,
                MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70,
                maturity_modulation
            );
            return;
        }
```

**File:** rs/nervous_system/governance/src/maturity_modulation/mod.rs (L11-29)
```rust
pub fn apply_maturity_modulation(
    amount_maturity_e8s: u64,
    maturity_modulation_basis_points: i32,
) -> Result<u64, String> {
    let amount_e8s = u128::from(amount_maturity_e8s);

    let adjusted_maturity_modulation_basis_points = saturating_add_or_subtract_u128_i32(
        BASIS_POINTS_PER_UNITY,
        maturity_modulation_basis_points,
    );

    let modulated_amount_e8s: u128 = amount_e8s
        .checked_mul(adjusted_maturity_modulation_basis_points)
        .ok_or_else(|| "Underflow or overflow when calculating maturity modulation".to_string())?
        .checked_div(BASIS_POINTS_PER_UNITY)
        .ok_or_else(|| "Underflow or overflow when calculating maturity modulation".to_string())?;

    u64::try_from(modulated_amount_e8s).map_err(|err| err.to_string())
}
```
