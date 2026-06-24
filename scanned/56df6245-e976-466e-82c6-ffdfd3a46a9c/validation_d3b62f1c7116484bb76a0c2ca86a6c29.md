### Title
Stale Maturity Modulation Applied Without Freshness Check During Disbursement Finalization - (File: rs/nns/governance/src/governance/disburse_maturity.rs)

### Summary
`try_finalize_maturity_disbursement` in NNS Governance reads `heap_data.maturity_modulation.current_value_permyriad` to compute the ICP amount to mint, but never checks `updated_at_days_since_epoch` for staleness. If the XRC-backed daily timer task fails for multiple consecutive days, the stale modulation factor is silently applied to all pending disbursements, minting incorrect ICP amounts.

### Finding Description
The NNS Governance canister introduced a Mission 70 maturity modulation system (Proposal 141738, 141779) that fetches ICP/XDR rates from the Exchange Rate Canister (XRC) daily via `UpdateIcpXdrRateRelatedData`, stores them in `heap_data.maturity_modulation` (a `MaturityModulation` struct with fields `current_value_permyriad` and `updated_at_days_since_epoch`), and applies the computed modulation factor when finalizing maturity disbursements.

The disbursement finalization path in `try_finalize_maturity_disbursement` reads the modulation value as follows:

```rust
let maturity_modulation = governance
    .heap_data
    .maturity_modulation
    .as_ref()
    .and_then(|m| m.current_value_permyriad);
``` [1](#0-0) 

The field `updated_at_days_since_epoch` is **never consulted** here. The value is passed directly to `next_maturity_disbursement_to_finalize` and then to `apply_maturity_modulation`, which computes `amount_to_mint_e8s` — the actual ICP minted to the neuron owner's account. [2](#0-1) 

The `MaturityModulation` proto stores the staleness indicator: [3](#0-2) 

The daily timer task (`UpdateIcpXdrRateRelatedData`) is the only writer of this field. The CHANGELOG explicitly acknowledges that XRC failures are a real operational scenario: [4](#0-3) 

When XRC fails for multiple consecutive days, the timer task logs and skips the update, leaving `current_value_permyriad` unchanged and `updated_at_days_since_epoch` pointing to a past day. The disbursement finalizer has no mechanism to detect or reject this stale value. [5](#0-4) 

### Impact Explanation
`apply_maturity_modulation` multiplies the maturity amount by `(1 + modulation / 10_000)`. A stale modulation that is, for example, +200 permyriad (the maximum) when the current correct value should be −1000 permyriad (the minimum) causes the governance canister to mint up to 12% more ICP than it should. Conversely, a stale negative modulation causes under-minting. This is a direct ledger conservation violation: ICP is minted in incorrect amounts from the governance minting account, affecting every neuron whose disbursement finalizes while the modulation is stale. [6](#0-5) 

### Likelihood Explanation
XRC unavailability is an acknowledged operational reality (CHANGELOG Proposal 141771 was specifically about tolerating XRC failures). The timer task retries on failure with a 60-second interval but does not block disbursement finalization. A multi-day XRC outage — or a sustained sequence of `ForexInvalidTimestamp` / `RateLimited` errors — leaves the modulation stale for the entire outage duration. Any neuron holder whose 7-day disbursement window expires during this period is affected. No privileged access is required; the `DisburseMaturity` command is available to any neuron controller. [7](#0-6) 

### Recommendation
Before applying `current_value_permyriad` in `try_finalize_maturity_disbursement`, check that `updated_at_days_since_epoch` is within an acceptable staleness bound (e.g., ≤ 2 days old relative to `now / ONE_DAY_SECONDS`). If the modulation is stale beyond the threshold, return an error or skip finalization and retry later, rather than applying a potentially incorrect factor. A concrete check:

```rust
let current_day = now_seconds / ONE_DAY_SECONDS;
let modulation_age_days = current_day
    .saturating_sub(m.updated_at_days_since_epoch.unwrap_or(0));
if modulation_age_days > MAX_MODULATION_STALENESS_DAYS {
    return Err(FinalizeMaturityDisbursementError::StaleMaturityModulation);
}
```

### Proof of Concept
1. Neuron owner calls `DisburseMaturity` at day D. Disbursement is scheduled to finalize at day D+7.
2. XRC becomes unavailable starting at day D+1. The `UpdateIcpXdrRateRelatedData` timer logs failures and skips updates. `updated_at_days_since_epoch` remains at day D (or earlier).
3. At day D+7, the finalization timer fires. `try_finalize_maturity_disbursement` reads `current_value_permyriad` (now 7 days stale) without checking `updated_at_days_since_epoch`.
4. `apply_maturity_modulation` computes `amount_to_mint_e8s` using the stale factor. If the true current modulation differs by the maximum swing (−1000 to +200 permyriad = 1200 permyriad = 12%), the neuron owner receives up to 12% more or less ICP than the protocol intends.
5. The ledger mint is executed with the incorrect amount; no error is raised. [8](#0-7) [9](#0-8)

### Citations

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

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L557-575)
```rust
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
```

**File:** rs/nns/governance/src/gen/ic_nns_governance.pb.v1.rs (L3197-3204)
```rust
pub struct MaturityModulation {
    /// Current maturity modulation in permyriad (0.01% per unit).
    #[prost(int32, optional, tag = "1")]
    pub current_value_permyriad: ::core::option::Option<i32>,
    /// Day (days_since_epoch) when current_value_permyriad was last computed.
    #[prost(uint64, optional, tag = "2")]
    pub updated_at_days_since_epoch: ::core::option::Option<u64>,
}
```

**File:** rs/nns/governance/CHANGELOG.md (L29-34)
```markdown
## Fixed

* Tolerate XRC failures when updating maturity modulation: compute the average
  over available days using last-observation-carried-forward, and advance past
  days where XRC returns no rate so that a single persistent gap no longer
  stalls maturity modulation updates.
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L265-309)
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

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L417-428)
```rust
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

**File:** rs/nervous_system/clients/src/exchange_rate_canister_client.rs (L54-67)
```rust
                ExchangeRateError::ForexInvalidTimestamp => {
                    write!(f, "The request timestamp is invalid")
                }
                ExchangeRateError::ForexBaseAssetNotFound => {
                    write!(f, "The forex base asset could not be found")
                }
                ExchangeRateError::ForexQuoteAssetNotFound => {
                    write!(f, "The forex quote asset could not be found")
                }
                ExchangeRateError::ForexAssetsNotFound => {
                    write!(f, "The forex assets could not be found")
                }
                ExchangeRateError::RateLimited => {
                    write!(f, "Request has been rate limited")
```
