### Title
Stale Maturity Modulation Rate Applied to ICP Minting Without Freshness Check - (`rs/nns/governance/src/governance.rs` and `rs/nns/governance/src/governance/disburse_maturity.rs`)

### Summary

The NNS Governance canister stores a `MaturityModulation` value that includes both `current_value_permyriad` (the rate) and `updated_at_days_since_epoch` (when it was last computed). Both `maybe_spawn_neurons()` and `try_finalize_maturity_disbursement()` read and apply `current_value_permyriad` to ICP minting operations without ever consulting `updated_at_days_since_epoch` to verify the value is fresh. If the daily XRC-fed update task (`UpdateIcpXdrRateRelatedData`) fails for multiple consecutive days, the stale modulation factor is silently applied to all neuron spawning and maturity disbursement operations, causing incorrect ICP amounts to be minted.

### Finding Description

The `MaturityModulation` protobuf message carries two fields:

```
message MaturityModulation {
  optional int32 current_value_permyriad = 1;
  optional uint64 updated_at_days_since_epoch = 2;
}
```

The `UpdateIcpXdrRateRelatedData` recurring task fetches ICP/XDR rates from the Exchange Rate Canister (XRC) daily and recomputes `current_value_permyriad`, stamping `updated_at_days_since_epoch` on success. If the XRC canister is unavailable for one or more days, the task logs an error and leaves the prior modulation value untouched — by design.

The consumption side, however, never checks the age of the cached value. In `maybe_spawn_neurons()`:

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

And in `try_finalize_maturity_disbursement()`:

```rust
let maturity_modulation = governance
    .heap_data
    .maturity_modulation
    .as_ref()
    .and_then(|m| m.current_value_permyriad);
```

Neither call site reads `updated_at_days_since_epoch`. The only guard applied is a bounds check (`VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE`), which verifies the value is within [-500, 500] permyriad — not that it is recent. A value that is days or weeks old and still within bounds passes silently. [1](#0-0) [2](#0-1) [3](#0-2) 

### Impact Explanation

`apply_maturity_modulation()` multiplies the neuron's maturity by `(1 + current_value_permyriad / 10_000)` to determine the ICP amount minted. A stale modulation factor that no longer reflects the current ICP/XDR price ratio causes every neuron spawn and every maturity disbursement finalization to mint an incorrect amount of ICP. The error is bounded to ±5% of the maturity amount per operation, but for large neurons or high-volume periods this represents a material ledger conservation deviation — ICP is either over-minted or under-minted relative to what the protocol intends. [4](#0-3) [5](#0-4) [6](#0-5) 

### Likelihood Explanation

The XRC canister is an IC system canister. Its unavailability — due to subnet issues, canister bugs, or prolonged `Pending` / `RateLimited` responses — is a realistic operational scenario that requires no attacker. The `UpdateIcpXdrRateRelatedData` task already documents that it tolerates gaps via last-observation-carried-forward (LOCF), meaning the stale value is intentionally preserved across failures. Any neuron owner with a spawning neuron or a pending maturity disbursement whose finalization window falls during a multi-day XRC outage is automatically affected by the periodic task without any privileged action. [7](#0-6) [8](#0-7) 

### Recommendation

Before applying `current_value_permyriad` in `maybe_spawn_neurons()` and `try_finalize_maturity_disbursement()`, check that `updated_at_days_since_epoch` is within an acceptable staleness window (e.g., ≤ 2 days behind `now / ONE_DAY_SECONDS`). If the value is too stale, either skip the operation and reschedule, or fall back to the neutral 0-permyriad value. This mirrors the fix pattern used in the Chainlink analog: verify the `updatedAt` timestamp against the current time before trusting the returned price.

```rust
// Example guard (pseudo-code):
let current_day = now_seconds / ONE_DAY_SECONDS;
let modulation = match &self.heap_data.maturity_modulation {
    Some(mm) if mm.updated_at_days_since_epoch
                   .map(|d| current_day.saturating_sub(d) <= MAX_STALE_DAYS)
                   .unwrap_or(false) => mm.current_value_permyriad.unwrap_or(0),
    _ => 0, // fall back to neutral if stale or absent
};
``` [9](#0-8) 

### Proof of Concept

1. The NNS Governance canister initializes `maturity_modulation` with `current_value_permyriad: Some(0)` and `updated_at_days_since_epoch: None`.
2. After several days of successful XRC fetches, `current_value_permyriad` is set to, say, `+300` (permyriad) and `updated_at_days_since_epoch` to day `D`.
3. The XRC canister becomes unavailable for 10 days. `UpdateIcpXdrRateRelatedData::execute()` logs errors and leaves `current_value_permyriad = 300`, `updated_at_days_since_epoch = D` unchanged.
4. On day `D+10`, a neuron with `maturity_e8s_equivalent = 1_000_000_000` (10 ICP) reaches its `spawn_at_timestamp_seconds`. `maybe_spawn_neurons()` fires, reads `current_value_permyriad = 300` without checking `updated_at_days_since_epoch`, and mints `1_000_000_000 * (10_300 / 10_000) = 1_030_000_000` e8s — 3% more ICP than the current market-aligned modulation would dictate.
5. The same stale factor is applied to every concurrent maturity disbursement finalization via `try_finalize_maturity_disbursement()`. [10](#0-9) [11](#0-10) [12](#0-11)

### Citations

**File:** rs/nns/governance/src/governance.rs (L6421-6447)
```rust
    pub async fn maybe_spawn_neurons(&mut self) {
        if !self.can_spawn_neurons() {
            return;
        }

        let now_seconds = self.env.now();
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

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L558-575)
```rust
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

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L386-428)
```rust
/// Recomputes maturity modulation from the current price history and updates `maturity_modulation`.
///
/// Tolerates gaps in the price history: averages use LOCF in `compute_average_icp_xdr_rate`. If
/// the buffer has no rate at or before any day in the recent window, the calculation returns
/// `Err` and the prior modulation value is preserved.
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
```

**File:** rs/nns/governance/src/heap_governance_data.rs (L224-232)
```rust
        // Default to a neutral 0 permyriad so that spawning and maturity disbursement keep
        // working immediately after init, before `update_icp_xdr_rate_related_data` accumulates
        // enough price history to compute a real one. `updated_at_days_since_epoch` is left
        // `None` so the task treats this as "no prior measurement" rather than "already updated
        // today".
        maturity_modulation: Some(MaturityModulation {
            current_value_permyriad: Some(0),
            updated_at_days_since_epoch: None,
        }),
```
