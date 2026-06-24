### Title
CMC Maturity Modulation Silently Uses Zero for Any 7-Day Interval With Missing XRC Rates, Skewing the Average Toward Zero - (`rs/nns/cmc/src/main.rs`)

### Summary
`compute_capped_maturity_modulation` in the Cycles Minting Canister returns `0` for any 7-day interval where the XRC failed to deliver a rate for the required start or end day. The outer `compute_maturity_modulation` always divides the sum of four such intervals by 4, regardless of how many intervals actually had valid data. This is the direct IC analog of the Perennial bug: a missing oracle value silently becomes zero and is used unchanged in a financial accumulation formula.

### Finding Description
`compute_maturity_modulation` computes the final modulation as the arithmetic mean of four consecutive 7-day intervals:

```rust
fn compute_maturity_modulation(rates: &[IcpXdrConversionRate], time_s: u64) -> i32 {
    let day = time_s / 86_400;
    let rate1 = compute_capped_maturity_modulation(rates, day - 7,  day);
    let rate2 = compute_capped_maturity_modulation(rates, day - 14, day - 7);
    let rate3 = compute_capped_maturity_modulation(rates, day - 21, day - 14);
    let rate4 = compute_capped_maturity_modulation(rates, day - 28, day - 21);
    (rate1 + rate2 + rate3 + rate4) / 4   // always divides by 4
}
``` [1](#0-0) 

Each helper uses a circular buffer keyed by `day % ICP_XDR_CONVERSION_RATE_CACHE_SIZE`. If the XRC failed to deliver a rate for the required day, the slot holds a stale entry from a previous cycle. The day-equality check fails and the function returns `0`:

```rust
if start_day == day_at_start_index && end_day == day_at_end_index {
    // compute relative change
} else {
    0   // ← silent zero, same as Perennial's price=0
}
``` [2](#0-1) 

The outer function always divides by 4, so one missing interval reduces the modulation to 75 % of its correct value; two missing intervals reduce it to 50 %; all four missing reduce it to 0. No warning is logged and no fallback to the last valid rate (LOCF) is applied.

The result is stored in `state.maturity_modulation_permyriad` and returned by the `neuron_maturity_modulation()` query endpoint, which is callable by any unprivileged principal. [3](#0-2) 

The governance canister historically read this value via `cached_daily_maturity_modulation_basis_points` to drive neuron spawning and maturity disbursement. As of Proposal 141779 (2026-05-17) the NNS governance switched to its own locally-computed Mission 70 modulation (which correctly uses LOCF via `compute_average_icp_xdr_rate`). [4](#0-3) 

However, the CMC's `compute_maturity_modulation` / `compute_capped_maturity_modulation` code path is still live: it runs on every call to `update_recent_icp_xdr_rates`, its result is persisted in CMC state, and `neuron_maturity_modulation()` still exposes it. Any external canister, wallet, or dashboard that queries the CMC for the modulation value and uses it to compute ICP amounts will receive a silently under-estimated value whenever XRC has gaps. [5](#0-4) 

### Impact Explanation
When the XRC fails to deliver a rate for one or more of the four 7-day reference days (a realistic occurrence given rate-limiting, `StablecoinRateTooFewRates`, or transient network errors), the CMC's published maturity modulation is silently biased toward zero. Any consumer of `neuron_maturity_modulation()` that uses the value to scale an ICP mint or disbursement will compute a smaller-than-correct ICP amount for neuron holders, constituting a ledger conservation bug: neuron holders receive less ICP than the protocol intends.

### Likelihood Explanation
The CMC calls the XRC every five minutes. The XRC can return errors (`StablecoinRateTooFewRates`, `RateLimited`, `Pending`, etc.) or simply be unreachable. A single missed rate for a boundary day of any of the four intervals is sufficient to zero-out that interval's contribution. Given the 28-day lookback window and the frequency of transient XRC failures observed in practice, at least one interval will have a missing boundary rate on a non-trivial fraction of days.

### Recommendation
Replace the hard-coded `0` return in `compute_capped_maturity_modulation` with a LOCF fallback: when the exact day is absent, use the most recent prior rate in the buffer, mirroring the approach already implemented in the governance canister's `compute_average_icp_xdr_rate`. Alternatively, deprecate and remove the CMC's own modulation computation entirely now that governance computes it locally with correct gap-handling.

### Proof of Concept
1. XRC returns `StablecoinRateTooFewRates` for day D (the "end" boundary of the most-recent 7-day interval).
2. The CMC's circular buffer slot for day D still holds the rate from day `D - ICP_XDR_CONVERSION_RATE_CACHE_SIZE` (a stale entry).
3. `compute_capped_maturity_modulation(rates, D-7, D)` evaluates `D == day_at_end_index` → `false` → returns `0`.
4. `compute_maturity_modulation` computes `(0 + rate2 + rate3 + rate4) / 4` instead of `(rate1 + rate2 + rate3 + rate4) / 4`.
5. `neuron_maturity_modulation()` returns a value that is ≈25 % lower than the correct modulation.
6. Any external consumer that mints ICP proportional to `maturity * (1 + modulation/10000)` mints less ICP than the protocol intends, permanently under-compensating neuron holders for that disbursement window.

### Citations

**File:** rs/nns/cmc/src/main.rs (L918-941)
```rust
fn update_recent_icp_xdr_rates(state: &mut State, new_rate: &IcpXdrConversionRate) {
    let day = new_rate.timestamp_seconds / 86_400;
    // The index is the day modulo `ICP_XDR_CONVERSION_RATE_CACHE_SIZE`.
    let index = (day as usize) % ICP_XDR_CONVERSION_RATE_CACHE_SIZE;

    let recent_rates = state.recent_icp_xdr_rates.get_or_insert(vec![
        IcpXdrConversionRate::default();
        ICP_XDR_CONVERSION_RATE_CACHE_SIZE
    ]);

    // The record is updated if it is the first entry of a new day or an earlier
    // entry of the same day.
    let day_at_index = recent_rates[index].timestamp_seconds / 86_400;
    if day_at_index < day
        || (day_at_index == day
            && recent_rates[index].timestamp_seconds > new_rate.timestamp_seconds)
    {
        recent_rates[index] = new_rate.clone();
        // Update the average ICP/XDR rate and the maturity modulation.
        let time = now_seconds();
        state.average_icp_xdr_conversion_rate =
            compute_average_icp_xdr_rate_at_time(recent_rates, time);
        state.maturity_modulation_permyriad = Some(compute_maturity_modulation(recent_rates, time));
    }
```

**File:** rs/nns/cmc/src/main.rs (L1042-1048)
```rust
/// The function returns the current maturity modulation in basis points.
#[query(hidden = true)]
fn neuron_maturity_modulation() -> Result<i32, String> {
    Ok(with_state(|state| {
        state.maturity_modulation_permyriad.unwrap_or(0)
    }))
}
```

**File:** rs/nns/cmc/src/main.rs (L1052-1061)
```rust
fn compute_maturity_modulation(rates: &[IcpXdrConversionRate], time_s: u64) -> i32 {
    let day = time_s / 86_400;
    // Get the rate for four seven-day periods.
    let rate1 = compute_capped_maturity_modulation(rates, day - 7, day);
    let rate2 = compute_capped_maturity_modulation(rates, day - 14, day - 7);
    let rate3 = compute_capped_maturity_modulation(rates, day - 21, day - 14);
    let rate4 = compute_capped_maturity_modulation(rates, day - 28, day - 21);
    // Return the average as the final maturity modulation.
    (rate1 + rate2 + rate3 + rate4) / 4
}
```

**File:** rs/nns/cmc/src/main.rs (L1078-1100)
```rust
    // A proper modulation is only possible if we have rates for both days.
    // Otherwise, no modulation happens for this interval, i.e., zero is returned.
    if start_day == day_at_start_index && end_day == day_at_end_index {
        let start_rate_result = compute_average_icp_xdr_rate_at_time(rates, start_day * 86_400);
        let end_rate_result = compute_average_icp_xdr_rate_at_time(rates, end_day * 86_400);
        if let (Some(start_rate), Some(end_rate)) = (start_rate_result, end_rate_result) {
            let start_rate_value = start_rate.xdr_permyriad_per_icp as i32;
            let end_rate_value = end_rate.xdr_permyriad_per_icp as i32;
            let difference = end_rate_value.saturating_sub(start_rate_value);
            let difference_permyriad = difference.saturating_mul(10_000);
            match difference_permyriad.checked_div(start_rate_value) {
                Some(relative_change_permyriad) => relative_change_permyriad.clamp(
                    MIN_MATURITY_MODULATION_PERMYRIAD,
                    MAX_MATURITY_MODULATION_PERMYRIAD,
                ),
                None => 0,
            }
        } else {
            0
        }
    } else {
        0
    }
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L71-113)
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
```
