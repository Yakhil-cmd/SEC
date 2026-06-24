### Title
Incorrect ICP/XDR Price Persisted in `icp_price_history` Propagates to Maturity Modulation, Corrupting ICP Minted on Disbursement/Spawn - (File: `rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs`)

---

### Summary

The NNS Governance canister's `UpdateIcpXdrRateRelatedData` timer task fetches daily ICP/XDR rates from the Exchange Rate Canister (XRC) and stores them permanently in `icp_price_history`. The only validation applied is a source-count check (`validate_exchange_rate`) and a zero-value check. There is no sanity check on the magnitude of the rate itself. A plausible-but-wildly-incorrect rate (e.g., 1 XDR per ICP instead of 10 XDR per ICP, or 1000 XDR per ICP) that passes the source-count threshold is permanently written into the 365-day price history buffer. Once written, it is never corrected — the `get_day_to_fetch` logic skips days already present in the buffer. This corrupted rate then drives the `compute_maturity_modulation_permyriad` calculation, which directly controls how many ICP tokens are minted when neuron owners disburse or spawn maturity.

---

### Finding Description

**Entry path:**

The `UpdateIcpXdrRateRelatedData` task runs as a recurring timer in the NNS Governance canister. Each execution calls `fetch_and_validate_rate`, which:

1. Calls `xrc_client.get_icp_to_xdr_exchange_rate(Some(timestamp))` for a specific historical day.
2. Calls `validate_exchange_rate(&exchange_rate)` — this only checks that `base_asset_num_received_rates >= MINIMUM_ICP_SOURCES (4)` and `quote_asset_num_received_rates >= MINIMUM_CXDR_SOURCES (4)`.
3. Checks that the returned timestamp matches the requested timestamp.
4. Checks that the converted permyriad value is non-zero.
5. If all checks pass, calls `update_rates_buffer(history, rate)` to **permanently insert** the rate into `icp_price_history`. [1](#0-0) 

The `validate_exchange_rate` function checks only source counts, not the rate value itself: [2](#0-1) 

Once a rate is inserted, `get_day_to_fetch` will never return that day again (it skips days already present in the buffer): [3](#0-2) 

The `update_rates_buffer` function does allow overwriting an existing entry for the same timestamp, but this path is explicitly noted as "shouldn't happen" and is only triggered if the same day is fetched twice — which the cursor logic prevents: [4](#0-3) 

The corrupted rate then feeds `compute_maturity_modulation_permyriad`, which computes a 7-day vs. 365-day average ratio: [5](#0-4) 

The LOCF (Last Observation Carried Forward) mechanism amplifies the damage: a single corrupted rate is carried forward to fill all subsequent missing days in the averaging window: [6](#0-5) 

The resulting `maturity_modulation.current_value_permyriad` is read directly by both `maybe_spawn_neurons` and `try_finalize_maturity_disbursement` to compute the ICP amount minted: [7](#0-6) [8](#0-7) 

The maturity modulation is bounded to `[-1000, +200]` permyriad (i.e., -10% to +2%): [9](#0-8) 

However, the bounds are applied to the **computed modulation**, not to the input price. A corrupted price that is 10× too high or too low will drive the modulation to its maximum bound (+200 or -1000 permyriad) and keep it pinned there for up to 365 days (the full lookback window), since the corrupted rate persists in the buffer and is never evicted until it ages out.

---

### Impact Explanation

**Governance ledger conservation bug / incorrect ICP minting:**

- If a corrupted high rate (e.g., 10× actual) is stored for a recent day, the 7-day average will be much higher than the 365-day average, driving maturity modulation to its maximum of +200 permyriad (+2%). Every neuron disbursement and spawn during this period mints 2% more ICP than correct.
- If a corrupted low rate is stored, modulation is driven to -1000 permyriad (-10%), causing every disbursement to mint 10% fewer ICP than correct.
- The corrupted rate persists in `icp_price_history` for up to 365 days (until `evict_stale_rates` removes it). There is no mechanism to correct a stored rate without a canister upgrade or governance proposal that directly patches state.
- The LOCF mechanism means a single corrupted rate can dominate the 7-day window for multiple consecutive days if subsequent fetches fail.
- The `icp_price_history` field is persisted across upgrades via `reassemble_governance_proto` / `split_governance_proto`, so the corruption survives canister upgrades. [10](#0-9) [11](#0-10) 

---

### Likelihood Explanation

The XRC canister aggregates rates from multiple exchanges via HTTPS outcalls. The IC's HTTPS outcalls mechanism requires consensus among subnet nodes on the response body. However:

1. The XRC canister itself can return a rate that passes the source-count threshold (≥4 sources) but is numerically incorrect due to a bug in its own aggregation logic, a stale cache, or a transient data anomaly at the exchanges it queries.
2. The `validate_exchange_rate` function only checks source counts — it does not check whether the rate is within any plausible range relative to previously stored rates or a known floor/ceiling.
3. The CMC's own `do_set_icp_xdr_conversion_rate` has a `DivergedRate` mechanism that can disable automatic XRC updates when a rate diverges from a governance-submitted rate. The Governance canister's new `UpdateIcpXdrRateRelatedData` task has **no equivalent divergence check**. [12](#0-11) 

The Governance task has no such protection: [13](#0-12) 

---

### Recommendation

1. **Add a plausibility range check** in `fetch_and_validate_rate`: reject rates that deviate by more than a configurable factor (e.g., 5×) from the most recently stored rate or from a known floor (analogous to `MIN_XDRS_PER_ICP = 1` used in SNS governance).
2. **Add a correction path**: allow `update_rates_buffer` to be called via a governance proposal or admin path to overwrite a specific day's rate, analogous to the CMC's `set_icp_xdr_conversion_rate` with `DivergedRate` reason.
3. **Mirror the CMC's divergence-disable mechanism**: if a fetched rate deviates beyond a threshold from the governance-submitted rate in the CMC, pause the `UpdateIcpXdrRateRelatedData` task until manually re-enabled.

---

### Proof of Concept

**Scenario:** XRC returns a rate of `1_000` permyriad (0.1 XDR/ICP) for day D, while the true rate is `100_000` permyriad (10 XDR/ICP). The rate passes `validate_exchange_rate` (source counts are fine) and the non-zero check.

1. `fetch_and_validate_rate(D * 86400)` returns `SampledPrice { timestamp_seconds: D*86400, xdr_permyriad_per_icp: 1_000 }`.
2. `update_rates_buffer` inserts this into `icp_price_history`.
3. `get_day_to_fetch` will never return day D again.
4. On the next midnight, `compute_maturity_modulation_permyriad` is called:
   - 7-day average includes day D's rate of `1_000`; if the other 6 days are `100_000`, the 7-day average ≈ `85_857`.
   - 365-day average ≈ `99_726` (364 days at `100_000`, 1 day at `1_000`).
   - `target = 2500 * (85857 - 99726) / 99726 ≈ -348` permyriad.
   - Speed-limited to `-30` permyriad per day from a prior value of 0.
5. Over subsequent days, if LOCF carries the corrupted rate forward (because subsequent fetches for days D+1, D+2 fail), the 7-day average drops further, driving modulation toward `-1000` permyriad.
6. All neuron disbursements during this period mint up to 10% fewer ICP than correct, permanently reducing neuron owners' ICP balances. [6](#0-5) [14](#0-13) [15](#0-14)

### Citations

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L46-50)
```rust
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

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L130-197)
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
    };

    // Global bounds have final say. The result is within [MIN, MAX] which fit in i64, so the
    // cast is safe.
    Ok(speed_limited.clamp(
        MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as i128,
        MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70 as i128,
    ) as i64)
}
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L222-260)
```rust
    /// Returns the oldest missing day in `[current_day - 364, current_day]` that is strictly
    /// greater than `self.last_attempted_day_in_round`, or `None` if no such day exists (either the
    /// history is complete or every missing day in the window has been attempted this round).
    ///
    /// Walks the sorted rates slice and the expected day range together in O(n).
    fn get_day_to_fetch(&self, current_day: u64) -> Option<u64> {
        self.governance.with_borrow(|gov| {
            let icp_xdr_rates = &gov
                .heap_data
                .icp_price_history
                .as_ref()
                .map(|h| &h.icp_xdr_rates[..])
                .unwrap_or(&[]);
            let oldest_needed = current_day.saturating_sub(MAX_RATES_BUFFER_SIZE as u64 - 1);
            let start_day = match self.last_attempted_day_in_round {
                Some(d) => d.saturating_add(1).max(oldest_needed),
                None => oldest_needed,
            };
            if start_day > current_day {
                return None;
            }
            let mut rate_idx = icp_xdr_rates
                .partition_point(|r| r.timestamp_seconds < start_day * ONE_DAY_SECONDS);
            for day in start_day..=current_day {
                let midnight = day * ONE_DAY_SECONDS;
                while rate_idx < icp_xdr_rates.len()
                    && icp_xdr_rates[rate_idx].timestamp_seconds < midnight
                {
                    rate_idx += 1;
                }
                if rate_idx >= icp_xdr_rates.len()
                    || icp_xdr_rates[rate_idx].timestamp_seconds != midnight
                {
                    return Some(day);
                }
                rate_idx += 1;
            }
            None
        })
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L263-309)
```rust
    /// Fetches the ICP/XDR rate from XRC for `timestamp`, validates, and converts.
    /// Returns `None` if any step fails (errors are logged).
    async fn fetch_and_validate_rate(&self, timestamp: u64) -> Option<SampledPrice> {
        let exchange_rate = match self
            .xrc_client
            .get_icp_to_xdr_exchange_rate(Some(timestamp))
            .await
        {
            Ok(rate) => rate,
            Err(err) => {
                println!(
                    "{}UpdateIcpXdrRateRelatedData: XRC call failed: {}",
                    LOG_PREFIX, err
                );
                return None;
            }
        };

        if let Err(err) = validate_exchange_rate(&exchange_rate) {
            println!(
                "{}UpdateIcpXdrRateRelatedData: XRC rate failed validation: {}",
                LOG_PREFIX, err
            );
            return None;
        }

        // Verify that XRC returned a rate for the day we requested. If not, the rate
        // won't fill the expected slot and backfill would loop on the same day.
        if exchange_rate.timestamp != timestamp {
            println!(
                "{}UpdateIcpXdrRateRelatedData: requested timestamp {} but XRC returned {}; ignoring.",
                LOG_PREFIX, timestamp, exchange_rate.timestamp
            );
            return None;
        }

        let rate = SampledPrice::from(&exchange_rate);
        if rate.xdr_permyriad_per_icp == 0 {
            println!(
                "{}UpdateIcpXdrRateRelatedData: received zero XDR/ICP rate; ignoring.",
                LOG_PREFIX
            );
            return None;
        }

        Some(rate)
    }
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L343-364)
```rust
fn update_rates_buffer(icp_price_history: &mut IcpPriceHistory, new_rate: SampledPrice) {
    let rates = &mut icp_price_history.icp_xdr_rates;

    match rates.binary_search_by_key(&new_rate.timestamp_seconds, |r| r.timestamp_seconds) {
        Ok(pos) => {
            // Shouldn't happen: we only fetch days absent from the buffer.
            println!(
                "{}update_rates_buffer: replacing existing entry for timestamp {} (old={}, new={})",
                LOG_PREFIX,
                new_rate.timestamp_seconds,
                rates[pos].xdr_permyriad_per_icp,
                new_rate.xdr_permyriad_per_icp,
            );
            rates[pos] = new_rate;
        }
        Err(pos) => {
            // Insert the new rate into the already-sorted vector at the correct position (O(n)
            // shift). New rates usually arrive in order, so pos == rates.len() is the common case.
            rates.insert(pos, new_rate);
        }
    }
}
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L497-513)
```rust
        let maybe_rate = self
            .fetch_and_validate_rate(day_to_fetch * ONE_DAY_SECONDS)
            .await;
        self.last_attempted_day_in_round = Some(day_to_fetch);

        let Some(rate) = maybe_rate else {
            return (Duration::from_secs(ERROR_RETRY_INTERVAL_SECONDS), self);
        };

        // Insert new/missing exchange rate into price history.
        self.governance.with_borrow_mut(|gov| {
            let history = gov
                .heap_data
                .icp_price_history
                .get_or_insert_with(IcpPriceHistory::default);
            update_rates_buffer(history, rate);
        });
```

**File:** rs/nervous_system/clients/src/exchange_rate_canister_client.rs (L110-129)
```rust
/// Validates that an ICP/CXDR exchange rate has enough sources.
pub fn validate_exchange_rate(
    exchange_rate: &ExchangeRate,
) -> Result<(), ValidateExchangeRateError> {
    if exchange_rate.metadata.base_asset_num_received_rates < MINIMUM_ICP_SOURCES {
        return Err(ValidateExchangeRateError::NotEnoughIcpSources {
            received: exchange_rate.metadata.base_asset_num_received_rates,
            queried: exchange_rate.metadata.base_asset_num_queried_sources,
        });
    }

    if exchange_rate.metadata.quote_asset_num_received_rates < MINIMUM_CXDR_SOURCES {
        return Err(ValidateExchangeRateError::NotEnoughCxdrSources {
            received: exchange_rate.metadata.quote_asset_num_received_rates,
            queried: exchange_rate.metadata.quote_asset_num_queried_sources,
        });
    }

    Ok(())
}
```

**File:** rs/nns/governance/src/governance.rs (L6427-6487)
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

        // Acquire the global "spawning" lock.
        self.heap_data.spawning_neurons = Some(true);

        // Filter all the neurons that are currently in "spawning" state.
        // Do this here to avoid having to borrow *self while we perform changes below.
        // Spawning neurons must have maturity, and no neurons in stable storage should have maturity.
        let ready_to_spawn_ids = self
            .neuron_store
            .list_ready_to_spawn_neuron_ids(now_seconds);

        // We can't alias ready_to_spawn_ids in the loop below, but the TLA model needs access to it,
        // so we clone it here.
        #[cfg(feature = "tla")]
        let mut _tla_ready_to_spawn_ids: BTreeSet<u64> =
            ready_to_spawn_ids.iter().map(|nid| nid.id).collect();

        for neuron_id in ready_to_spawn_ids {
            // Actually mint the neuron's ICP.
            let in_flight_command = NeuronInFlightCommand {
                timestamp: now_seconds,
                command: Some(InFlightCommand::Spawn(neuron_id)),
            };

            // Add the neuron to the set of neurons undergoing ledger updates.
            match self.lock_neuron_for_command(neuron_id.id, in_flight_command.clone()) {
                Ok(mut lock) => {
                    // Since we're multiplying a potentially pretty big number by up to 10500, do
                    // the calculations as u128 before converting back.
                    let neuron = self
                        .with_neuron(&neuron_id, |neuron| neuron.clone())
                        .expect("Neuron should exist, just found in list");

                    let original_maturity = neuron.maturity_e8s_equivalent;
                    let subaccount = neuron.subaccount();

                    let neuron_stake: u64 = match apply_maturity_modulation(
                        original_maturity,
                        maturity_modulation,
                    ) {
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

**File:** rs/nns/governance/src/heap_governance_data.rs (L49-50)
```rust
    pub icp_price_history: Option<IcpPriceHistory>,
    pub maturity_modulation: Option<MaturityModulation>,
```

**File:** rs/nns/governance/src/heap_governance_data.rs (L365-366)
```rust
        icp_price_history,
        maturity_modulation,
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L291-329)
```rust
pub fn set_update_exchange_rate_state(
    safe_state: &'static LocalKey<RefCell<Option<State>>>,
    maybe_reason: &Option<UpdateIcpXdrConversionRatePayloadReason>,
    rate_timestamp_seconds: u64,
) {
    if let Some(reason) = maybe_reason {
        mutate_state(safe_state, |state| {
            let current_update_exchange_rate_state = state
                .update_exchange_rate_canister_state
                .unwrap_or_default();
            match reason {
                UpdateIcpXdrConversionRatePayloadReason::EnableAutomaticExchangeRateUpdates => {
                    if current_update_exchange_rate_state == UpdateExchangeRateState::Disabled {
                        state.update_exchange_rate_canister_state.replace(
                            UpdateExchangeRateState::get_rate_at_next_refresh_rate_interval(
                                rate_timestamp_seconds,
                            ),
                        );
                    }
                }
                UpdateIcpXdrConversionRatePayloadReason::DivergedRate => {
                    state
                        .update_exchange_rate_canister_state
                        .replace(UpdateExchangeRateState::Disabled);
                }
                UpdateIcpXdrConversionRatePayloadReason::OldRate => {
                    if current_update_exchange_rate_state == UpdateExchangeRateState::Disabled {
                        return;
                    }

                    state.update_exchange_rate_canister_state.replace(
                        UpdateExchangeRateState::get_rate_at_next_refresh_rate_interval(
                            rate_timestamp_seconds,
                        ),
                    );
                }
            }
        });
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
