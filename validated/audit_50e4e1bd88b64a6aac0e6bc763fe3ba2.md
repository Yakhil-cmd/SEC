### Title
Maturity Modulation Oracle Accepts Single Observation for Multi-Day Window, Enabling Extreme ICP Minting Rates on First Calculation - (File: rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs)

### Summary
`compute_average_icp_xdr_rate` has no minimum observation count requirement: it returns a valid average from as few as 1 data point via Last Observation Carried Forward (LOCF), even when the window spans 7 or 365 days. Combined with `compute_maturity_modulation_permyriad` bypassing the daily speed limit on the first calculation, the maturity modulation can jump to its extreme bounds (−10% to +2%) based on a single XRC data point. This directly affects how much ICP is minted when neurons spawn or disburse maturity.

### Finding Description

`compute_average_icp_xdr_rate` iterates over all days in the window and uses LOCF to fill gaps. The only guard against returning a result is `count == 0`: [1](#0-0) 

If only 1 observation exists anywhere in the window, LOCF carries it forward for all remaining days, and the function returns `Some(that_single_value)`. The code itself logs a warning but does not reject the result: [2](#0-1) 

This is explicitly tested and confirmed as intended behavior — a single rate at day 96 in a 7-day window (days 94–100) returns `Some(80_000)` over only 5 contributing days: [3](#0-2) 

`compute_maturity_modulation_permyriad` then compounds this by skipping the daily speed limit entirely on the first calculation (`previous == None`): [4](#0-3) 

The execution flow is: after all 365 backfill attempts complete in a single round (regardless of how many succeeded), `update_maturity_modulation` is called unconditionally: [5](#0-4) 

If the majority of XRC fetches failed (e.g., XRC temporarily unavailable during initial deployment), the modulation is computed from very few observations, and the first-calculation path jumps directly to the target without smoothing.

The resulting `current_value_permyriad` is then consumed directly by neuron spawning: [6](#0-5) 

and maturity disbursement: [7](#0-6) 

### Impact Explanation

**Impact: Medium.** Maturity modulation is bounded globally at [−1000, +200] permyriad (−10% to +2%): [8](#0-7) 

If the modulation is computed from a single extreme observation (e.g., ICP price just crashed), all neurons spawning or disbursing maturity during that window receive up to 10% less ICP than they should. This is a ledger conservation issue: ICP minted does not reflect the true multi-day price average the algorithm is designed to compute.

### Likelihood Explanation

**Likelihood: Low.** The condition requires either:
1. A fresh governance canister deployment where the 365-day backfill has not yet completed, AND many XRC fetches fail in the same round, OR
2. An extended period of XRC unavailability causing the buffer to be sparse.

An unprivileged neuron owner can observe the publicly queryable `get_maturity_modulation` endpoint and time their `spawn_neuron` or `disburse_maturity` call to coincide with a favorable extreme modulation value caused by sparse observations. They cannot directly cause the sparse-observation condition, but they can exploit it opportunistically.

### Recommendation

1. Enforce a minimum observation count in `compute_average_icp_xdr_rate` before returning a result. For the 7-day window, require at least 4 observations; for the 365-day window, require at least a configurable threshold (e.g., 30 days).
2. Do not skip the speed limit on the first calculation when the observation count is below the minimum threshold — treat an under-populated buffer as insufficient rather than as a valid first-calculation baseline.
3. Alternatively, defer the first modulation computation until the buffer holds at least a minimum number of days of data, rather than computing immediately after the first backfill round completes.

### Proof of Concept

```
// Single observation in a 7-day window → returns Some(value) with no rejection:
let rates = vec![SampledPrice {
    timestamp_seconds: 96 * ONE_DAY_SECONDS,
    xdr_permyriad_per_icp: 80_000,
}];
assert_eq!(compute_average_icp_xdr_rate(&rates, 100, 7), Some(80_000));
// count=5, window_days=7 → warning logged but result returned

// First calculation with sparse data skips speed limit:
// If recent_price >> reference_price, target_modulation can be +200 permyriad immediately.
// All neurons spawning at this moment receive 2% more ICP than the algorithm intends.
``` [9](#0-8) [10](#0-9)

### Citations

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L47-50)
```rust
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000;

/// Upper bound for Mission 70 maturity modulation: +2% = 200 permyriad.
pub(crate) const MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70: i64 = 200;
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L71-114)
```rust
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
}
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L130-162)
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

    let speed_limited = match previous {
        // First calculation: no baseline to smooth from, so jump straight to target.
        None => target_modulation,
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L461-491)
```rust
        let Some(day_to_fetch) = self.get_day_to_fetch(current_day) else {
            // Every missing day in the lookback window has been attempted this round (or none
            // was missing to begin with). Compute maturity modulation with what we have — gaps
            // are tolerated via LOCF in compute_average_icp_xdr_rate — then sleep until the next
            // midnight, when a fresh round retries any days that are still missing.
            self.governance.with_borrow_mut(|gov| {
                let data = &mut gov.heap_data;
                let Some(icp_price_history) = data.icp_price_history.as_ref() else {
                    println!(
                        "{}UpdateIcpXdrRateRelatedData: icp_price_history is None; \
                         skipping modulation update.",
                        LOG_PREFIX
                    );
                    return;
                };
                let maturity_modulation = data
                    .maturity_modulation
                    .get_or_insert_with(MaturityModulation::default);
                update_maturity_modulation(icp_price_history, maturity_modulation, current_day);
                println!(
                    "{}UpdateIcpXdrRateRelatedData: maturity modulation {} permyriad \
                     (day={}, buffer_size={})",
                    LOG_PREFIX,
                    maturity_modulation.current_value_permyriad.unwrap_or(0),
                    current_day,
                    icp_price_history.icp_xdr_rates.len(),
                );
            });

            self.last_attempted_day_in_round = None;
            return (duration_until_next_midnight_utc(now), self);
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data_tests.rs (L378-388)
```rust
#[test]
fn test_compute_average_icp_xdr_rate_skips_days_with_no_prior_rate() {
    // 7-day window ending at day 100 (days 94..=100). The earliest rate in the buffer is day 96.
    // Days 94, 95 have no prior rate to carry forward → skipped. Days 96-100 all use day 96's
    // rate via LOCF (the only rate). Average = 80_000 over 5 contributing days.
    let rates = vec![SampledPrice {
        timestamp_seconds: 96 * ONE_DAY_SECONDS,
        xdr_permyriad_per_icp: 80_000,
    }];
    assert_eq!(compute_average_icp_xdr_rate(&rates, 100, 7), Some(80_000));
}
```

**File:** rs/nns/governance/src/governance.rs (L6427-6435)
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
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L502-512)
```rust
    let maturity_to_disburse_after_modulation_e8s = apply_maturity_modulation(
        original_maturity_e8s_equivalent,
        maturity_modulation_basis_points,
    )
    .map_err(
        |reason| FinalizeMaturityDisbursementError::MaturityModulationFailure {
            maturity_before_modulation_e8s: original_maturity_e8s_equivalent,
            maturity_modulation_basis_points,
            reason,
        },
    )?;
```
