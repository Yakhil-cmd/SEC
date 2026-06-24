### Title
Stale Maturity Modulation Applied Without Freshness Check During Neuron Spawn and Maturity Disbursement — (`rs/nns/governance/src/governance.rs`, `rs/nns/governance/src/governance/disburse_maturity.rs`)

---

### Summary

The NNS Governance canister computes a `MaturityModulation` value daily from ICP/XDR price history fetched from the Exchange Rate Canister (XRC). This modulation is applied when minting ICP from neuron maturity (spawning and disbursement). Neither `maybe_spawn_neurons()` nor `try_finalize_maturity_disbursement()` checks the `updated_at_days_since_epoch` field of the stored `MaturityModulation` before applying it. If the XRC is unavailable for an extended period, the modulation value is frozen at its last computed state and applied indefinitely to all maturity conversions, minting the wrong amount of ICP.

---

### Finding Description

The `MaturityModulation` protobuf message stores two fields: [1](#0-0) 

The `updated_at_days_since_epoch` field records when `current_value_permyriad` was last computed. The daily timer task `UpdateIcpXdrRateRelatedData` fetches ICP/XDR rates from the XRC and calls `update_maturity_modulation()`, which sets both fields: [2](#0-1) 

When the XRC is unavailable, `fetch_and_validate_rate()` returns `None`, the buffer is not updated, and `update_maturity_modulation()` is not called. The `current_value_permyriad` remains frozen at its last computed value. The LOCF (Last Observation Carried Forward) mechanism in `compute_average_icp_xdr_rate` carries the last known rate forward for all missing days: [3](#0-2) 

This means the modulation is computed as if the ICP price never changed, regardless of how many days have elapsed since the last real observation.

**At the consumption site, no freshness check is performed.** In `maybe_spawn_neurons()`, the code reads only `current_value_permyriad`: [4](#0-3) 

Similarly, `try_finalize_maturity_disbursement()` reads only `current_value_permyriad`: [5](#0-4) 

The `updated_at_days_since_epoch` field is never consulted at either consumption site to determine whether the modulation is stale. The only place it is used is to prevent double-updating on the same day: [6](#0-5) 

---

### Impact Explanation

Maturity modulation directly controls how much ICP is minted when a neuron owner's maturity is converted: [7](#0-6) 

The modulation range is ±500 permyriad (±5%). If the XRC is unavailable for days or weeks and the ICP price moves significantly during that period, the frozen modulation value will be systematically wrong for all maturity conversions that finalize during the outage. Neuron owners whose disbursements mature during the outage receive the wrong amount of ICP — either too much (if price dropped but modulation is still positive) or too little (if price rose but modulation is still negative). This is a ledger conservation bug: ICP is minted at an incorrect rate relative to actual market conditions. [8](#0-7) 

---

### Likelihood Explanation

The IC has experienced subnet downtime during upgrades and other events. The XRC is hosted on a separate subnet from the NNS governance canister. Any period of XRC unavailability — including routine subnet upgrades, replica bugs, or transient network partitions — causes the modulation to freeze. Neuron maturity disbursements have a mandatory 7-day delay, so any XRC outage lasting more than a day will affect disbursements that finalize during or after the outage. The entry path requires only an unprivileged neuron owner calling `DisburseMaturity` or `SpawnNeuron` — standard governance operations available to any neuron holder. [9](#0-8) 

---

### Recommendation

Before applying `current_value_permyriad` in `maybe_spawn_neurons()` and `try_finalize_maturity_disbursement()`, check that `updated_at_days_since_epoch` is within an acceptable staleness bound (e.g., no more than N days behind `current_day`). If the modulation is too stale, either refuse to finalize (returning an error or deferring) or apply a conservative fallback value (e.g., 0 permyriad, meaning no modulation). The `updated_at_days_since_epoch` field already exists in the struct and is populated by `update_maturity_modulation()` — it simply needs to be checked at the consumption sites. [10](#0-9) 

---

### Proof of Concept

1. XRC subnet experiences downtime for 10 days.
2. `UpdateIcpXdrRateRelatedData::execute()` calls `fetch_and_validate_rate()` each day, which returns `None` due to XRC unavailability.
3. `update_maturity_modulation()` is never called; `maturity_modulation.current_value_permyriad` remains at its pre-outage value (e.g., `+200` permyriad, reflecting a previously high ICP price).
4. During the outage, ICP price drops 30%.
5. A neuron owner's 7-day maturity disbursement matures on day 8 of the outage.
6. `try_finalize_maturity_disbursement()` reads `current_value_permyriad = 200` (stale, positive) and mints `maturity * 1.02` ICP — more than warranted given the actual price drop.
7. The `updated_at_days_since_epoch` field (now 10 days stale) is never consulted. [11](#0-10) [12](#0-11)

### Citations

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L2079-2094)
```text
// The maturity modulation factor is applied when disbursing (unstaked) maturity to ICP.
//
// When a neuron owner disburses maturity, the amount of ICP received is:
//   maturity * (1 + current_value_permyriad / 10_000)
//
// This factor stabilizes ICP price: it is positive when ICP is above its long-term average
// (encouraging selling pressure), and negative when below (discouraging selling).
//
// This might be unpopulated, which indicates that no value is currently available.
message MaturityModulation {
  // Current maturity modulation in permyriad (0.01% per unit).
  optional int32 current_value_permyriad = 1;

  // Day (days_since_epoch) when current_value_permyriad was last computed.
  optional uint64 updated_at_days_since_epoch = 2;
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

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L391-429)
```rust
fn update_maturity_modulation(
    icp_price_history: &IcpPriceHistory,
    maturity_modulation: &mut MaturityModulation,
    current_day: u64,
) {
    if maturity_modulation.updated_at_days_since_epoch == Some(current_day) {
        return;
    }

    let previous = match (
        maturity_modulation.current_value_permyriad,
        maturity_modulation.updated_at_days_since_epoch,
    ) {
        (Some(p), Some(d)) => Some((p as i64, d)),
        _ => None,
    };

    match compute_maturity_modulation_permyriad(
        &icp_price_history.icp_xdr_rates,
        current_day,
        previous,
    ) {
        Ok(new_permyriad) => {
            maturity_modulation.current_value_permyriad = Some(new_permyriad as i32);
            maturity_modulation.updated_at_days_since_epoch = Some(current_day);
        }
        Err(reason) => {
            // Reaches this branch only when the buffer has no rate at or before any day in the
            // recent window (e.g., a fresh canister where every backfill fetch has failed so far,
            // or every fetched rate was zero). Log and leave the prior modulation untouched —
            // subsequent rounds will retry the missing days.
            println!(
                "{}update_maturity_modulation: skipping update: {}; leaving prior modulation \
                 unchanged",
                LOG_PREFIX, reason
            );
        }
    }
}
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L494-503)
```rust
        // Attempt the next missing day. Advance the cursor whether the fetch succeeds or fails
        // so the next tick moves on instead of looping on a day that keeps failing. Failed days
        // will be retried by the next midnight's fresh round (until they fall out of the window).
        let maybe_rate = self
            .fetch_and_validate_rate(day_to_fetch * ONE_DAY_SECONDS)
            .await;
        self.last_attempted_day_in_round = Some(day_to_fetch);

        let Some(rate) = maybe_rate else {
            return (Duration::from_secs(ERROR_RETRY_INTERVAL_SECONDS), self);
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

**File:** rs/nns/governance/src/governance.rs (L6484-6488)
```rust
                    let neuron_stake: u64 = match apply_maturity_modulation(
                        original_maturity,
                        maturity_modulation,
                    ) {
                        Ok(neuron_stake) => neuron_stake,
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

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L561-575)
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
    });
```
