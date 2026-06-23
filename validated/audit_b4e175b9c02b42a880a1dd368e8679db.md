### Title
Stale ICP/XDR Price Data Applied to ICP Minting Without Freshness Check at Point of Use — (File: `rs/nns/governance/src/governance/disburse_maturity.rs`, `rs/nns/governance/src/governance.rs`)

---

### Summary

The NNS Governance canister (post-Proposal 141779) applies `heap_data.maturity_modulation.current_value_permyriad` to ICP minting in both `maybe_spawn_neurons` and `finalize_maturity_disbursement` without ever checking the companion `updated_at_days_since_epoch` field. The modulation is computed from a 7-day moving average with Last-Observation-Carried-Forward (LOCF) gap-filling and a hard 30-permyriad/day speed limit. Neither mechanism can immediately reflect a sharp ICP price drop. During any period in which the Exchange Rate Canister (XRC) fails to return fresh rates, the LOCF logic silently carries the last known (pre-drop) rate forward into the "recent" window, and the speed limit prevents the modulation from correcting even when rates do arrive. Because no staleness guard exists at the minting call sites, neuron holders who initiate maturity disbursement during such a window receive more ICP than the current price warrants.

---

### Finding Description

**Price-history pipeline (the oracle layer)**

`UpdateIcpXdrRateRelatedData::execute` runs once per day and calls `compute_average_icp_xdr_rate` for two windows:
- *Recent*: 7-day moving average (`MATURITY_MODULATION_CURRENT_ICP_PRICE_WINDOW_DAYS = 7`)
- *Reference*: 365-day moving average (`MATURITY_MODULATION_REFERENCE_ICP_PRICE_WINDOW_DAYS = 365`) [1](#0-0) 

For any day whose XRC fetch failed, `compute_average_icp_xdr_rate` carries the most-recent prior rate forward (LOCF): [2](#0-1) 

If XRC is unavailable for days N+1 through N+6, the entire 7-day "recent" window is filled with day N's rate. The modulation is then computed as if the price has not moved.

Additionally, a hard speed limit of 30 permyriad/day is enforced: [3](#0-2) 

Even when fresh rates arrive, the modulation can only move 30 permyriad per day toward the true target. A 50% ICP price drop would take ~33 days to fully propagate to the minimum modulation of −1 000 permyriad.

**Minting call sites — no staleness check**

`maybe_spawn_neurons` reads the modulation directly: [4](#0-3) 

`try_finalize_maturity_disbursement` does the same: [5](#0-4) 

Neither site inspects `updated_at_days_since_epoch`. The `MaturityModulation` proto carries this field: [6](#0-5) 

but it is only used inside the timer task to skip double-updates on the same calendar day: [7](#0-6) 

It is never consulted before `apply_maturity_modulation` is called: [8](#0-7) [9](#0-8) 

**Initialization default**

On canister init the modulation is seeded to 0 permyriad with `updated_at_days_since_epoch: None`: [10](#0-9) 

This means all disbursements and spawns proceed at neutral modulation until 365 days of XRC history accumulate — a window that can span months on a fresh deployment.

---

### Impact Explanation

`apply_maturity_modulation` scales the minted ICP amount by `(1 + modulation / 10_000)`: [11](#0-10) 

The modulation range is [−1 000, +200] permyriad (−10 % to +2 %): [12](#0-11) 

If the modulation is stale at +200 permyriad when the true value should be −1 000 permyriad, every disbursement mints approximately 12 % more ICP than intended. For large neuron holders this is a material ledger conservation loss: ICP is minted without corresponding economic justification, diluting all token holders. The 7-day disbursement delay (`DISBURSEMENT_DELAY_SECONDS`) does not protect against this because the stale modulation is applied at *finalization* time, not at initiation time. [13](#0-12) 

---

### Likelihood Explanation

Two independent conditions each produce the stale-modulation window:

1. **XRC unavailability**: If XRC returns errors for 2–6 consecutive days, LOCF fills the 7-day recent window with the pre-drop rate. The timer retries every 60 seconds on failure, but if XRC is down for a full day the daily modulation update is skipped and the prior value is preserved unchanged.

2. **Speed-limit lag**: Even with perfect XRC availability, a sharp ICP price drop (e.g., 30 %+ in one day) cannot be reflected in the modulation for weeks due to the 30-permyriad/day cap. Any neuron holder who monitors the modulation value and initiates disbursement immediately after a sharp drop will receive the pre-drop modulation at finalization 7 days later.

Both conditions are observable on-chain. A governance user can query `get_maturity_modulation` to confirm the modulation is stale or lagging before initiating disbursement. [14](#0-13) 

---

### Recommendation

1. **Add a staleness guard at the minting call sites.** Before applying `current_value_permyriad`, check that `updated_at_days_since_epoch` is within an acceptable number of days of the current day (e.g., ≤ 2 days). If the modulation is too stale, block disbursement finalization and neuron spawning until it is refreshed, rather than silently using an outdated value.

2. **Consider a tighter speed limit or a secondary "shock absorber" check.** If the 7-day recent price deviates from the reference price by more than a threshold in a single day, allow the modulation to move faster (or immediately) to the new target, analogous to using a spot price alongside the moving average.

3. **Emit an on-chain metric or log** when the modulation has not been updated for more than one day, so that NNS monitoring can detect and respond to XRC outages before significant maturity disbursements finalize.

---

### Proof of Concept

**Scenario (speed-limit lag path — no XRC failure required):**

1. ICP/XDR rate is 10 XDR for the past 365 days. Modulation is 0 permyriad.
2. On day D, ICP price drops to 7 XDR (−30 %). XRC returns this rate successfully.
3. The timer updates the modulation: target ≈ 2 500 × (7 − 10) / 10 = −750 permyriad. Speed limit clamps the change to −30 permyriad. New modulation: −30 permyriad.
4. A neuron holder with 10 000 ICP worth of maturity calls `DisburseMaturity` on day D.
5. Over the next 7 days the modulation moves to −210 permyriad (7 × 30). The true target remains −750 permyriad.
6. On day D+7, `finalize_maturity_disbursement` fires. Modulation applied: −210 permyriad. ICP minted: `10 000 × (1 − 0.021) = 9 790 ICP`.
7. If the modulation had fully reflected the price drop (−750 permyriad), the user would have received `10 000 × (1 − 0.075) = 9 250 ICP`.
8. **Excess ICP minted: 540 ICP per 10 000 ICP of maturity** — a 5.8 % overpayment relative to the true price-adjusted amount, with no attacker action beyond initiating a standard `DisburseMaturity` call at the right moment. [15](#0-14)

### Citations

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L43-44)
```rust
/// Maximum daily change in maturity modulation: 0.3% = 30 permyriad.
const MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD: i64 = 30;
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L47-50)
```rust
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000;

/// Upper bound for Mission 70 maturity modulation: +2% = 200 permyriad.
pub(crate) const MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70: i64 = 200;
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L56-57)
```rust
/// Retry delay after a transient XRC failure. Short so we recover quickly without hammering XRC.
const ERROR_RETRY_INTERVAL_SECONDS: u64 = 60;
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

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L396-398)
```rust
    if maturity_modulation.updated_at_days_since_epoch == Some(current_day) {
        return;
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

**File:** rs/nns/governance/src/governance.rs (L6484-6487)
```rust
                    let neuron_stake: u64 = match apply_maturity_modulation(
                        original_maturity,
                        maturity_modulation,
                    ) {
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L36-37)
```rust
/// The delay in seconds between initiating a maturity disbursement and the actual disbursement.
const DISBURSEMENT_DELAY_SECONDS: u64 = ONE_DAY_SECONDS * 7;
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

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L561-574)
```rust
    let (maturity_disbursement_finalization, now_seconds) = governance.with_borrow(|governance| {
        let now_seconds = governance.env.now();
        let maturity_modulation = governance
            .heap_data
            .maturity_modulation
            .as_ref()
            .and_then(|m| m.current_value_permyriad);
        let maturity_disbursement_finalization = next_maturity_disbursement_to_finalize(
            &governance.neuron_store,
            &governance.heap_data.in_flight_commands,
            maturity_modulation,
            now_seconds,
        );
        (maturity_disbursement_finalization, now_seconds)
```

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L2088-2094)
```text
message MaturityModulation {
  // Current maturity modulation in permyriad (0.01% per unit).
  optional int32 current_value_permyriad = 1;

  // Day (days_since_epoch) when current_value_permyriad was last computed.
  optional uint64 updated_at_days_since_epoch = 2;
}
```

**File:** rs/nns/governance/src/heap_governance_data.rs (L229-232)
```rust
        maturity_modulation: Some(MaturityModulation {
            current_value_permyriad: Some(0),
            updated_at_days_since_epoch: None,
        }),
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
