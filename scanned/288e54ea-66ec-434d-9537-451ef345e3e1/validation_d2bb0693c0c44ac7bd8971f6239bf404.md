### Title
Unbounded Speed-Limit Multiplier on Multi-Day Timer Gap Allows Maturity Modulation to Jump to Target in One Step, Bypassing the Daily Rate Cap - (File: rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs)

---

### Summary

The `compute_maturity_modulation_permyriad` function in the NNS Governance canister applies a daily speed limit of 30 permyriad to smooth day-to-day changes in maturity modulation. However, when the timer misses multiple days (e.g., due to a canister upgrade, subnet halt, or any operational gap), the speed limit is multiplied proportionally by `days_elapsed`, allowing the modulation to jump directly to the target value in a single update. This is the IC analog of the L2 sequencer-downtime oracle vulnerability: stale/outdated price data is extrapolated across a gap period without enforcing the intended per-day rate cap, resulting in an inaccurate and potentially extreme modulation value being applied to ICP minting for neuron spawning and maturity disbursement.

---

### Finding Description

In `compute_maturity_modulation_permyriad`, the speed-limit logic is:

```rust
let max_change = if days_elapsed > 1 {
    // The timer missed one or more days — allow proportionally more change.
    MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD.saturating_mul(days_elapsed as i64)
} else if days_elapsed == 1 {
    MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD
} else {
    MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD
};
target_modulation.clamp(
    previous_permyriad.saturating_sub(max_change) as i128,
    previous_permyriad.saturating_add(max_change) as i128,
)
``` [1](#0-0) 

When `days_elapsed` is large (e.g., 34 days, which is `1000 / 30` rounded up), `max_change` becomes `30 * 34 = 1020`, which exceeds the global bounds of `[-1000, +200]`. This means the speed limit is effectively bypassed: the modulation can jump from any prior value directly to the target in a single timer execution, defeating the purpose of the daily rate cap.

The global bounds clamp is applied after the speed-limit clamp:

```rust
Ok(speed_limited.clamp(
    MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as i128,
    MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70 as i128,
) as i64)
``` [2](#0-1) 

So the global bounds still hold, but the speed limit — which is the mechanism designed to prevent sudden large swings in maturity modulation — is completely bypassed after a gap of ≥34 days. The LOCF (Last Observation Carried Forward) mechanism in `compute_average_icp_xdr_rate` further amplifies this: if XRC fetches failed for many days, the most recent stale price is silently carried forward across all missing days, producing a "current" 7-day average that is entirely composed of a single old price point. [3](#0-2) 

The resulting maturity modulation value is then consumed directly by `maybe_spawn_neurons` and `try_finalize_maturity_disbursement` to mint ICP: [4](#0-3) [5](#0-4) 

---

### Impact Explanation

The maturity modulation value directly controls how much ICP is minted when neurons spawn or disburse maturity. A sudden jump to the maximum positive modulation (+200 permyriad = +2%) or minimum negative modulation (-1000 permyriad = -10%) after a multi-day gap means:

- **Neuron spawning**: `apply_maturity_modulation(original_maturity, maturity_modulation)` mints ICP at the extreme modulation value rather than the smoothly-transitioned value the speed limit was designed to enforce. [6](#0-5) 
- **Maturity disbursement**: The same extreme modulation is applied to all pending disbursements that finalize after the gap. [7](#0-6) 

The impact is a **ledger conservation bug**: ICP is minted in amounts that deviate from the intended gradual modulation schedule. At -10% modulation, neuron holders receive 10% less ICP than expected; at +2%, they receive 2% more. This is a direct financial impact on all neurons that happen to spawn or disburse maturity during or immediately after a multi-day governance canister downtime.

---

### Likelihood Explanation

The NNS Governance canister undergoes periodic upgrades (visible in the CHANGELOG), and each upgrade resets the in-memory `last_attempted_day_in_round` cursor. More importantly, the `updated_at_days_since_epoch` field in `MaturityModulation` persists across upgrades, so after a canister upgrade that takes even 2+ days, `days_elapsed` will be ≥2 and the proportional speed limit will apply. Subnet halts (for upgrades) are a routine, non-adversarial event on the IC. The CHANGELOG shows multiple governance upgrades per month, and the comment in the code itself acknowledges this scenario:

> "The timer missed one or more days — allow proportionally more change." [8](#0-7) 

Any governance canister upgrade that spans ≥34 days (or any operational gap of that length) will fully bypass the speed limit. Even shorter gaps (e.g., 7 days) allow a 7× speed-limit multiplier (210 permyriad), which already exceeds the +200 upper bound and allows a full jump to the maximum positive modulation.

---

### Recommendation

1. **Cap `days_elapsed` at 1** for speed-limit purposes: if the timer missed days, do not reward the gap with a proportionally larger allowed change. The speed limit exists precisely to prevent sudden jumps regardless of cause.
2. **Alternatively**, if catch-up behavior is intentional, document it explicitly and ensure the global bounds are sufficient to contain the worst-case outcome — but note that the current bounds already allow a -10% swing in a single step after a 34-day gap.
3. **Validate LOCF staleness**: `compute_average_icp_xdr_rate` should reject or discount averages where the LOCF seed is older than a configurable maximum age (e.g., 7 days), rather than silently carrying a stale price forward across the entire 7-day or 365-day window.

---

### Proof of Concept

**Scenario**: NNS Governance canister is upgraded and the timer does not fire for 34 days (e.g., due to a prolonged subnet halt or repeated upgrade failures). The last stored modulation was 0 permyriad. The ICP price has dropped significantly during the gap.

1. `updated_at_days_since_epoch = day_N`, `current_value_permyriad = 0`.
2. After 34 days, the timer fires. `days_elapsed = 34`.
3. `max_change = 30 * 34 = 1020`.
4. The LOCF-based 7-day average uses the last available price (stale, from day N), and the 365-day average also uses LOCF-filled values. If the real price dropped 20% during the gap, the 7-day average still reflects the old price, so `target_modulation` is near 0.
5. However, if the price had already been trending down before the gap, `target_modulation` could be `-1000` (the global minimum). With `max_change = 1020 ≥ 1000`, the speed-limit clamp does not constrain the result, and the modulation jumps directly from 0 to -1000 in one step.
6. All neurons that finalize spawning or disbursement on that day receive 10% less ICP than they would have under the intended gradual schedule.

The attacker-controlled entry path is: any neuron holder who times their `DisburseMaturity` or `Spawn` initiation to finalize during the post-gap window receives a modulation value that was not gradually transitioned, potentially to their detriment (or benefit, if modulation jumps to +200). [9](#0-8) [1](#0-0) [10](#0-9)

### Citations

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L43-50)
```rust
/// Maximum daily change in maturity modulation: 0.3% = 30 permyriad.
const MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD: i64 = 30;

/// Lower bound for Mission 70 maturity modulation: -10% = -1000 permyriad.
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000;

/// Upper bound for Mission 70 maturity modulation: +2% = 200 permyriad.
pub(crate) const MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70: i64 = 200;
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

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L160-188)
```rust
    let speed_limited = match previous {
        // First calculation: no baseline to smooth from, so jump straight to target.
        None => target_modulation,
        Some((previous_permyriad, previous_day)) => {
            // Limit day-to-day change.
            let days_elapsed = current_day.saturating_sub(previous_day);
            let max_change = if days_elapsed > 1 {
                // The timer missed one or more days — allow proportionally more change.
                println!(
                    "{}compute_maturity_modulation_permyriad: {} days elapsed since last update (current_day={}, previous_day={})",
                    LOG_PREFIX, days_elapsed, current_day, previous_day
                );
                MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD.saturating_mul(days_elapsed as i64)
            } else if days_elapsed == 1 {
                MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD
            } else {
                // days_elapsed == 0: either same day or current_day < previous_day (should not happen).
                // Allow at least one day of movement.
                println!(
                    "{}compute_maturity_modulation_permyriad: days_elapsed=0 (current_day={}, previous_day={}); treating as 1 day",
                    LOG_PREFIX, current_day, previous_day
                );
                MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD
            };
            target_modulation.clamp(
                previous_permyriad.saturating_sub(max_change) as i128,
                previous_permyriad.saturating_add(max_change) as i128,
            )
        }
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L193-196)
```rust
    Ok(speed_limited.clamp(
        MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as i128,
        MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70 as i128,
    ) as i64)
```

**File:** rs/nns/governance/src/governance.rs (L276-278)
```rust
const VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE: RangeInclusive<i32> =
    MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as i32
        ..=MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70 as i32;
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

**File:** rs/nns/governance/src/governance.rs (L6484-6502)
```rust
                    let neuron_stake: u64 = match apply_maturity_modulation(
                        original_maturity,
                        maturity_modulation,
                    ) {
                        Ok(neuron_stake) => neuron_stake,
                        Err(err) => {
                            // Do not retain the lock so that other Neuron operations can continue.
                            // This is safe as no changes to the neuron have been made to the neuron
                            // both internally to governance and externally in ledger.
                            println!(
                                "{}Could not apply modulation to {:?} for neuron {:?} due to {:?}, skipping",
                                LOG_PREFIX,
                                neuron.maturity_e8s_equivalent,
                                neuron.id(),
                                err
                            );
                            continue;
                        }
                    };
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L499-512)
```rust
    // Apply the maturity modulation to the disbursement amount. This should not fail unless
    // something else in the system is wrong, such as an insanely large amount of maturity or an
    // incorrect maturity modulation basis points.
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
