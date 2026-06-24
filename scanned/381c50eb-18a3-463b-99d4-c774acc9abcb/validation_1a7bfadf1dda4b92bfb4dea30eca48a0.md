### Title
Stale ICP/XDR Price Used for Maturity Modulation After LOCF Seed Eviction Race - (File: rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs)

### Summary

The `evict_stale_rates` function in the NNS Governance canister intentionally retains one "LOCF seed" entry from before the 365-day lookback window. When this seed is the only rate in the buffer and it is very old (e.g., from canister initialization or a prolonged XRC outage), `compute_average_icp_xdr_rate` silently carries it forward across the entire 365-day window, producing a maturity modulation value derived entirely from a single arbitrarily old price. Since Proposal 141779 (2026-05-17), neuron spawning and maturity disbursement finalization in NNS Governance now consume this locally computed `maturity_modulation` value directly, making the stale-seed path financially impactful.

### Finding Description

`evict_stale_rates` keeps the single most-recent entry that predates the window as a LOCF seed:

```rust
let before_window = rates.partition_point(|r| r.timestamp_seconds < oldest_kept_seconds);
let drop_count = before_window.saturating_sub(1);
if drop_count > 0 {
    rates.drain(0..drop_count);
}
```

`compute_average_icp_xdr_rate` then carries this seed forward for every day in the window that has no fetched rate:

```rust
while rate_idx < rates.len() && rates[rate_idx].timestamp_seconds <= midnight {
    current_value = Some(rates[rate_idx].xdr_permyriad_per_icp);
    rate_idx += 1;
}
if let Some(v) = current_value {
    sum += v as u128;
    count += 1;
}
```

If the seed is the only entry (all 365 in-window fetches have failed or the canister is freshly deployed), the 7-day "recent" average and the 365-day "reference" average are both computed from this single stale value. The resulting `target_modulation` is `sensitivity * (seed - seed) / seed = 0`, so `compute_maturity_modulation_permyriad` returns `Ok(0)` — a neutral modulation — regardless of the actual current ICP price. This is not an error path; `update_maturity_modulation` writes `current_value_permyriad = Some(0)` and marks `updated_at_days_since_epoch = Some(current_day)`, freezing the modulation at zero for the entire day.

The frozen-at-zero modulation is then consumed by `maybe_spawn_neurons`:

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

A modulation of 0 is within `VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE` (`[-1000, 200]`), so the sanity check passes and spawning proceeds with the incorrect rate.

The root cause mirrors the external report exactly: a flag/guard mechanism (the LOCF seed) is designed to handle missing data, but when the seed itself is stale, clearing the "no data" condition (by having *some* entry in the buffer) causes the stale value to be used silently rather than surfacing an error.

### Impact Explanation

Neuron spawning and maturity disbursement finalization (since Proposal 141779) apply `maturity_modulation.current_value_permyriad` to convert maturity to ICP. A stale seed forces this to 0 permyriad regardless of the true ICP/XDR price. If the true modulation should be, e.g., +200 permyriad (+2%), all spawning neurons receive 2% less ICP than they are entitled to. Conversely, if the true modulation should be −1000 permyriad (−10%), neurons receive 10% more ICP than intended, diluting the supply. The error persists for the entire day (until the next midnight round) and affects every neuron that spawns or finalizes disbursement during that window.

### Likelihood Explanation

This condition is reachable without any privileged action. It occurs automatically whenever:
1. The Governance canister is freshly deployed or upgraded and the `icp_price_history` buffer is empty or sparse, **and**
2. The XRC is transiently unavailable for all 365 backfill fetches within a single daily round (e.g., during an XRC upgrade, network partition, or rate-limiting episode).

In that scenario `get_day_to_fetch` returns `None` after exhausting all attempts, `update_maturity_modulation` is called with only the LOCF seed, and the stale-zero modulation is committed. The XRC has historically experienced outages. The backfill window is 365 calls at 5-second intervals (~30 minutes), so a 30-minute XRC outage on the day of a Governance upgrade is sufficient to trigger this.

### Recommendation

1. **Distinguish "no data" from "only stale seed"**: In `compute_maturity_modulation_permyriad`, if the computed averages are identical (i.e., both windows return the same single carried-forward value and no in-window rate exists), return `Err` rather than `Ok(0)`.
2. **Alternatively**, in `update_maturity_modulation`, check whether the buffer contains at least one in-window entry before calling `compute_maturity_modulation_permyriad`; if not, skip the update and leave the prior modulation unchanged.
3. **Track seed age**: Record the timestamp of the LOCF seed and reject it if it is older than a configurable threshold (e.g., 7 days) for the purposes of the recent-price window.

### Proof of Concept

**Step 1 – Governance canister is freshly deployed; `icp_price_history` is `None`.**

**Step 2 – XRC is unavailable for the entire backfill round.** `fetch_and_validate_rate` returns `None` for every day. `last_attempted_day_in_round` advances to `current_day`. `get_day_to_fetch` returns `None`.

**Step 3 – `execute()` enters the "no missing day" branch:**

```rust
let Some(day_to_fetch) = self.get_day_to_fetch(current_day) else {
    self.governance.with_borrow_mut(|gov| {
        ...
        update_maturity_modulation(icp_price_history, maturity_modulation, current_day);
    });
    ...
};
``` [1](#0-0) 

**Step 4 – `icp_price_history` is `None`, so `update_maturity_modulation` is skipped entirely** (the `let Some(icp_price_history) = data.icp_price_history.as_ref() else { return; }` guard fires). `maturity_modulation` remains `None`.

**Step 5 – `maybe_spawn_neurons` reads `maturity_modulation`:**

```rust
let maturity_modulation = match self
    .heap_data
    .maturity_modulation
    .as_ref()
    .and_then(|m| m.current_value_permyriad)
{
    None => return,   // ← returns here; spawning is blocked entirely
    Some(value) => value,
};
``` [2](#0-1) 

**Step 6 – Spawning is blocked until the next successful XRC fetch.** This is the correct safe behavior for the fully-empty case. However, the vulnerability manifests in the **partial-seed case**:

**Step 6 (variant) – One stale seed exists** (e.g., from a prior successful fetch that has since rolled out of the window but was retained by `evict_stale_rates`): [3](#0-2) 

`icp_price_history` is `Some(...)` with one entry at timestamp `T_seed < oldest_kept_seconds`. `compute_average_icp_xdr_rate` carries `T_seed`'s value forward for all 365 days: [4](#0-3) 

Both the 7-day and 365-day averages equal `seed_value`. `target_modulation = 2500 * (seed - seed) / seed = 0`. `compute_maturity_modulation_permyriad` returns `Ok(0)`. [5](#0-4) 

`update_maturity_modulation` writes `current_value_permyriad = Some(0)` and `updated_at_days_since_epoch = Some(current_day)`: [6](#0-5) 

`maybe_spawn_neurons` reads `Some(0)`, passes the bounds check, and applies 0% modulation to all spawning neurons for the entire day — regardless of the true ICP/XDR price: [7](#0-6) 

The stale price is used because the LOCF seed clears the "no data" error condition without the code detecting that the seed itself is arbitrarily old, exactly analogous to the `citadelPriceFlag` being cleared while the stale `citadelPriceInAsset` continues to be used.

### Citations

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L87-113)
```rust
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

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L153-158)
```rust
    let target_modulation = {
        let recent = recent_icp_price as i128;
        let reference = reference_icp_price as i128;
        let sensitivity = MATURITY_MODULATION_SENSITIVITY_PERMYRIAD as i128;
        sensitivity * (recent - reference) / reference
    };
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L373-384)
```rust
fn evict_stale_rates(icp_price_history: &mut IcpPriceHistory, current_day: u64) {
    let oldest_kept_day = current_day.saturating_sub(MAX_RATES_BUFFER_SIZE as u64 - 1);
    let oldest_kept_seconds = oldest_kept_day * ONE_DAY_SECONDS;
    let rates = &mut icp_price_history.icp_xdr_rates;
    // Number of entries strictly before the window. We keep the most recent of these as the LOCF
    // seed; the rest (older ones) are dropped.
    let before_window = rates.partition_point(|r| r.timestamp_seconds < oldest_kept_seconds);
    let drop_count = before_window.saturating_sub(1);
    if drop_count > 0 {
        rates.drain(0..drop_count);
    }
}
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L413-415)
```rust
        Ok(new_permyriad) => {
            maturity_modulation.current_value_permyriad = Some(new_permyriad as i32);
            maturity_modulation.updated_at_days_since_epoch = Some(current_day);
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L461-492)
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
        };
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
