### Title
Missing Staleness Check on Cached Maturity Modulation Before ICP Minting — (`rs/nns/governance/src/governance/disburse_maturity.rs`)

### Summary

The NNS Governance canister reads a cached `maturity_modulation.current_value_permyriad` — derived from Exchange Rate Canister (XRC) ICP/XDR price history — to compute the exact ICP amount minted when finalizing neuron maturity disbursements and spawning neurons. Neither the disbursement finalization path nor the neuron-spawning path checks the `updated_at_days_since_epoch` field of the `MaturityModulation` struct before applying the rate. If the XRC becomes unavailable for an extended period, the daily `UpdateIcpXdrRateRelatedData` timer task silently stops refreshing the price history, the modulation value freezes at its last computed state, and all subsequent minting operations use a stale rate with no error or guard.

### Finding Description

The `MaturityModulation` protobuf message stored in `heap_data.maturity_modulation` carries two fields: `current_value_permyriad` (the rate) and `updated_at_days_since_epoch` (the day it was last computed). The daily timer task `UpdateIcpXdrRateRelatedData` in `rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs` fetches ICP/XDR rates from the XRC and recomputes the modulation. When the XRC is unreachable, `fetch_and_validate_rate` returns `None` and the task retries after `ERROR_RETRY_INTERVAL_SECONDS`, but it never marks the cached modulation as invalid or stale. [1](#0-0) 

The finalization path in `try_finalize_maturity_disbursement` reads the cached value directly: [2](#0-1) 

It extracts only `current_value_permyriad` and passes it to `next_maturity_disbursement_to_finalize`, which immediately applies it to compute the ICP amount to mint: [3](#0-2) 

The `updated_at_days_since_epoch` field is never read at this call site. The same pattern appears in `maybe_spawn_neurons`: [4](#0-3) 

Again, only `current_value_permyriad` is extracted; `updated_at_days_since_epoch` is ignored.

The `MaturityModulation` struct is updated exclusively by `update_maturity_modulation` inside the timer task: [5](#0-4) 

If the XRC is down for multiple consecutive days, `fetch_and_validate_rate` keeps returning `None`, the price history buffer stops receiving new entries, and `update_maturity_modulation` is never called with a new `current_day`. The modulation value is frozen at whatever it was when the XRC last responded, with no mechanism to surface this staleness to the minting logic.

The CMC's `neuron_maturity_modulation` query — the older path still used by SNS governance — also returns a cached value with no freshness check: [6](#0-5) 

### Impact Explanation

Every call to `finalize_maturity_disbursement` or `maybe_spawn_neurons` mints ICP using the stale modulation. The modulation is bounded to ±500 basis points (±5%), so the per-disbursement error is at most 5% of the maturity amount. Over a prolonged XRC outage, all neuron holders who disburse or spawn during that window receive an incorrect ICP amount — either systematically over-minted (if ICP price fell since the last update) or under-minted (if ICP price rose). Because the governance canister is the ICP minting authority, this directly affects ledger conservation: the total ICP supply diverges from what it would be under a correct modulation.

### Likelihood Explanation

The XRC is a system canister on the NNS subnet. A subnet stall, a canister upgrade failure, or a persistent `ForexInvalidTimestamp` / `StablecoinRateTooFewRates` error from the XRC's upstream HTTP outcalls can cause the XRC to return errors for hours or days. The CMC's `UpdateExchangeRateState::Disabled` path (triggered by a diverged-rate NNS proposal) can also freeze the CMC's rate, which SNS governance then inherits. Any neuron holder — an unprivileged ingress sender — can trigger disbursement or spawning at any time, so the stale rate is applied without any privileged action.

### Recommendation

Before applying `current_value_permyriad` in `try_finalize_maturity_disbursement` and `maybe_spawn_neurons`, check that `updated_at_days_since_epoch` is within an acceptable staleness window (e.g., ≤ 2 days from `now / ONE_DAY_SECONDS`). If the value is too old, defer the minting operation and log a warning rather than proceeding with a potentially stale rate. A similar guard should be added to the CMC's `neuron_maturity_modulation` query to return an explicit error when `maturity_modulation_permyriad` has not been refreshed within the expected interval, so callers (SNS governance) can detect and handle XRC unavailability rather than silently consuming a frozen value.

### Proof of Concept

1. The XRC becomes unavailable (e.g., persistent HTTP outcall failures or a `StablecoinRateTooFewRates` error).
2. `UpdateIcpXdrRateRelatedData::execute` calls `fetch_and_validate_rate`, receives `None`, and returns `ERROR_RETRY_INTERVAL_SECONDS` delay. This repeats indefinitely. `maturity_modulation.updated_at_days_since_epoch` is never advanced past its last value.
3. A neuron holder calls `DisburseMaturity` on NNS governance. After 7 days, `FinalizeMaturityDisbursementsTask` fires.
4. `try_finalize_maturity_disbursement` reads `heap_data.maturity_modulation.current_value_permyriad` — the frozen, stale value — without checking `updated_at_days_since_epoch`.
5. `apply_maturity_modulation(original_maturity_e8s, stale_modulation)` computes an incorrect ICP amount.
6. The ICP ledger mints that incorrect amount, permanently diverging the supply from the intended value. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L265-278)
```rust
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

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L497-503)
```rust
        let maybe_rate = self
            .fetch_and_validate_rate(day_to_fetch * ONE_DAY_SECONDS)
            .await;
        self.last_attempted_day_in_round = Some(day_to_fetch);

        let Some(rate) = maybe_rate else {
            return (Duration::from_secs(ERROR_RETRY_INTERVAL_SECONDS), self);
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

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L556-587)
```rust
/// Tries to finalize the maturity disbursement for the first neuron that is ready to be finalized.
/// Returns an error if there is anything unexpected.
async fn try_finalize_maturity_disbursement(
    governance: &'static LocalKey<RefCell<Governance>>,
) -> Result<(), FinalizeMaturityDisbursementError> {
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

    let Some(MaturityDisbursementFinalization {
        neuron_id,
        destination,
        amount_to_mint_e8s,
        original_maturity_e8s_equivalent,
        finalize_disbursement_timestamp_seconds,
    }) = maturity_disbursement_finalization?
    else {
        // No disbursement to finalize.
        return Ok(());
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

**File:** rs/nns/cmc/src/main.rs (L1043-1048)
```rust
#[query(hidden = true)]
fn neuron_maturity_modulation() -> Result<i32, String> {
    Ok(with_state(|state| {
        state.maturity_modulation_permyriad.unwrap_or(0)
    }))
}
```
