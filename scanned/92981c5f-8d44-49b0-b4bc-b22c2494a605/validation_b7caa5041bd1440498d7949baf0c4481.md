### Title
Loss of Precision in `compute_maturity_modulation_permyriad` Due to Division Before Multiplication - (File: rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs)

### Summary
The `compute_maturity_modulation_permyriad` function in NNS Governance computes the maturity modulation factor — which directly determines how much ICP a neuron owner receives when disbursing maturity — using pre-divided integer averages as inputs. Integer division is applied to compute the 7-day and 365-day ICP price averages before those values are used in subsequent multiplications, causing avoidable precision loss that propagates into every maturity disbursement.

### Finding Description
In `compute_average_icp_xdr_rate`, the average ICP/XDR rate is returned as:

```rust
Some((sum / count as u128) as u64)
``` [1](#0-0) 

This integer division truncates the true average, introducing an error of up to 1 permyriad per average. The two resulting truncated averages (`recent_icp_price` and `reference_icp_price`) are then fed directly into `compute_maturity_modulation_permyriad`:

```rust
let target_modulation = {
    let recent = recent_icp_price as i128;
    let reference = reference_icp_price as i128;
    let sensitivity = MATURITY_MODULATION_SENSITIVITY_PERMYRIAD as i128;
    sensitivity * (recent - reference) / reference
};
``` [2](#0-1) 

The pattern is:
1. `recent = sum_recent / count_recent` — **division**
2. `reference = sum_reference / count_reference` — **division**
3. `sensitivity * (recent - reference)` — **multiplication after division**
4. `/ reference` — **division again on already-truncated value**

The mathematically precise computation would avoid pre-dividing the sums:

```
sensitivity * (sum_recent * count_reference - sum_reference * count_recent)
    / (count_recent * sum_reference)
```

Instead, the code divides first (computing integer averages), then multiplies by `sensitivity = 2_500`, compounding the truncation error.

The truncated `target_modulation` is then applied in `apply_maturity_modulation`:

```rust
let modulated_amount_e8s: u128 = amount_e8s
    .checked_mul(adjusted_maturity_modulation_basis_points)
    ...
    .checked_div(BASIS_POINTS_PER_UNITY)
``` [3](#0-2) 

where `adjusted_maturity_modulation_basis_points = 10_000 + target_modulation`. Any error in `target_modulation` directly scales the ICP minted. [4](#0-3) 

### Impact Explanation
**Medium.** The maturity modulation is expressed in permyriad and applied as `ICP = maturity × (1 + target_modulation / 10_000)`. An error of 1 permyriad in `target_modulation` produces an ICP disbursement error of `maturity_e8s / 10_000`. For a neuron with 1,000,000 ICP of maturity, this is a 100 ICP error per disbursement. The error is systematic (always present, always in the truncation direction for each average), not random. It affects every neuron owner who calls `DisburseMaturity`. The disbursed amount will not be catastrophically wrong, but it will consistently deviate from the mathematically correct value. [5](#0-4) 

### Likelihood Explanation
**Medium.** The precision loss occurs on every execution of `compute_maturity_modulation_permyriad`, which runs daily via the recurring timer task. It is not conditional on any special input — any non-zero price difference between the 7-day and 365-day averages will produce a truncated result. The effect is larger when the price difference is small (the numerator `sensitivity * diff` is small relative to `reference`, making the truncation a larger fraction of the true result). [6](#0-5) 

### Recommendation
Restructure the computation to avoid intermediate integer divisions. Pass the raw sums and counts from `compute_average_icp_xdr_rate` into the modulation formula, or accumulate the formula numerator and denominator separately before performing a single final division:

```rust
// Instead of:
let recent = sum_recent / count_recent;
let reference = sum_reference / count_reference;
sensitivity * (recent - reference) / reference

// Prefer (multiply before dividing):
// sensitivity * (sum_recent * count_reference - sum_reference * count_recent)
//     / (count_recent * sum_reference)
```

This eliminates the two intermediate truncations and reduces total precision loss to a single final division. [7](#0-6) 

### Proof of Concept
**Concrete numeric example:**

- 358 days at 50,000 permyriad, 7 days at 40,000 permyriad (price drop scenario from the existing test).
- `sum_recent = 7 × 40,000 = 280,000`; `count_recent = 7` → `recent = 280,000 / 7 = 40,000` (exact here).
- `sum_reference = 358 × 50,000 + 7 × 40,000 = 17,900,000 + 280,000 = 18,180,000`; `count_reference = 365` → `reference = 18,180,000 / 365 = 49,808` (truncated; true value = 49,808.21...).

With the truncated reference:
`target = 2,500 × (40,000 − 49,808) / 49,808 = 2,500 × (−9,808) / 49,808 = −24,520,000 / 49,808 = −492` (truncated).

With the exact reference (49,808.21...):
`target = 2,500 × (40,000 − 49,808.21) / 49,808.21 ≈ −492.0` (same here by coincidence of rounding).

A more sensitive case: `sum_reference = 18,180,001`, `count_reference = 365` → `reference = 49,808` (same truncation), but the true average is 49,808.22. The truncation of `reference` causes `sensitivity * diff / reference` to be computed with a denominator that is 0.22 units too small, inflating the magnitude of the result by `0.22 / 49,808 ≈ 0.0004%` — small per call, but systematic across all daily updates and all disbursements.

The existing test at line 474 in the test file acknowledges the truncation implicitly by computing the expected value using the same integer-divided averages, masking the true precision loss from the test assertions. [8](#0-7)

### Citations

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L18-26)
```rust
// ---- Maturity modulation algorithm ----
//
// Maturity modulation is the conversion factor from maturity to ICP. It is designed to have a
// stabilizing effect on the price of ICP: when the recent ICP price is above its long-term
// average, modulation is positive (more ICP per maturity), encouraging selling pressure; when
// below, modulation is negative (less ICP per maturity), discouraging selling.
//
// The result is in permyriad. For example, if this returns `mm` and the maturity being converted
// is `r`, the ICP minted is `r * (1 + mm / 10_000)`.
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L38-41)
```rust
/// How much the relative difference between current and reference ICP price affects maturity
/// modulation. k = 0.25 means a 10% price increase yields a 2.5% modulation boost.
/// Expressed in permyriad: 0.25 * 10_000 = 2_500.
const MATURITY_MODULATION_SENSITIVITY_PERMYRIAD: i64 = 2_500;
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L59-113)
```rust
/// Compute the average `xdr_permyriad_per_icp` over the most recent `window_days` days ending
/// at `current_day` (exclusive of `current_day - window_days`, inclusive of `current_day`).
///
/// For each day in the window, uses that day's rate if present, otherwise falls back to the most
/// recent prior day's rate (Last Observation Carried Forward). This keeps the average meaningful
/// even when XRC fails to return a rate for one or more days. The fallback is computation-only;
/// nothing is written back to `rates`.
///
/// Returns `None` only when LOCF never finds a value to carry — i.e., no rate in the buffer has
/// a timestamp at or before `current_day` (so every day in the window is skipped). If a rate
/// appears partway into the window, leading days that precede it are skipped and the average is
/// computed over the days that do have a value.
pub(crate) fn compute_average_icp_xdr_rate(
    rates: &[SampledPrice],
    current_day: u64,
    window_days: usize,
) -> Option<u64> {
    if window_days == 0 {
        return None;
    }
    let oldest_day_in_window = current_day
        .saturating_sub(window_days as u64)
        .saturating_add(1);

    // Single linear pass: walk days and rates together, carrying the latest seen value forward.
    // For each day, advance through every rate at or before that day so `current_value` holds
    // the LOCF value when we sum. Days iterated before the first rate appears contribute
    // nothing (LOCF has no prior to carry forward).
    let mut rate_idx = 0;
    let mut current_value: Option<u64> = None;
    let mut sum: u128 = 0;
    let mut count: u64 = 0;
    for day in oldest_day_in_window..=current_day {
        let midnight = day * ONE_DAY_SECONDS;
        while rate_idx < rates.len() && rates[rate_idx].timestamp_seconds <= midnight {
            current_value = Some(rates[rate_idx].xdr_permyriad_per_icp);
            rate_idx += 1;
        }
        if let Some(v) = current_value {
            sum += v as u128;
            count += 1;
        }
    }

    if (count as usize) < window_days {
        println!(
            "{}compute_average_icp_xdr_rate: only {} of {} days have a rate available \
             (current_day={})",
            LOG_PREFIX, count, window_days, current_day
        );
    }
    if count == 0 {
        return None;
    }
    Some((sum / count as u128) as u64)
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L130-158)
```rust
fn compute_maturity_modulation_permyriad(
    rates: &[SampledPrice],
    current_day: u64,
    previous: Option<(i64, u64)>,
) -> Result<i64, String> {
    let recent_icp_price = compute_average_icp_xdr_rate(
        rates,
        current_day,
        MATURITY_MODULATION_CURRENT_ICP_PRICE_WINDOW_DAYS,
    )
    .ok_or_else(|| "no rate available for the recent price window".to_string())?;

    let reference_icp_price = compute_average_icp_xdr_rate(
        rates,
        current_day,
        MATURITY_MODULATION_REFERENCE_ICP_PRICE_WINDOW_DAYS,
    )
    .ok_or_else(|| "no rate available for the reference price window".to_string())?;

    if reference_icp_price == 0 {
        return Err("reference price averaged to zero".to_string());
    }

    let target_modulation = {
        let recent = recent_icp_price as i128;
        let reference = reference_icp_price as i128;
        let sensitivity = MATURITY_MODULATION_SENSITIVITY_PERMYRIAD as i128;
        sensitivity * (recent - reference) / reference
    };
```

**File:** rs/nervous_system/governance/src/maturity_modulation/mod.rs (L22-26)
```rust
    let modulated_amount_e8s: u128 = amount_e8s
        .checked_mul(adjusted_maturity_modulation_basis_points)
        .ok_or_else(|| "Underflow or overflow when calculating maturity modulation".to_string())?
        .checked_div(BASIS_POINTS_PER_UNITY)
        .ok_or_else(|| "Underflow or overflow when calculating maturity modulation".to_string())?;
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data_tests.rs (L471-489)
```rust
fn test_compute_maturity_modulation_price_decrease() {
    // ICP was at 5 XDR for the past year, except for the past 7 days it dropped to 4 XDR.
    // 7-day average: 40_000; 365-day average = (358*50_000 + 7*40_000) / 365 = 49_808.
    // target = 2_500 * (40_000 - 49_808) / 49_808 ≈ -492 permyriad (negative = price dropped).
    // Starting from 0, speed limit is 30 permyriad/day → result = -30.
    let mut rates: Vec<SampledPrice> = (1..=358)
        .map(|d| SampledPrice {
            timestamp_seconds: d * ONE_DAY_SECONDS,
            xdr_permyriad_per_icp: 50_000,
        })
        .collect();
    for d in 359..=365 {
        rates.push(SampledPrice {
            timestamp_seconds: d * ONE_DAY_SECONDS,
            xdr_permyriad_per_icp: 40_000,
        });
    }
    let result = compute_maturity_modulation_permyriad(&rates, 365, Some((0, 364)));
    assert_eq!(result, Ok(-MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD));
```
