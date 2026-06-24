### Title
Missing Staleness Check on ICP/XDR Rate Returned from CMC — (`rs/sns/governance/token_valuation/src/lib.rs`)

### Summary

The SNS governance canister fetches the ICP/XDR exchange rate from the Cycles Minting Canister (CMC) to compute SNS treasury valuations, but never validates the `timestamp_seconds` field of the returned rate. If the CMC's stored rate is stale (e.g., due to prolonged XRC unavailability or the XRC integration being disabled), the SNS treasury valuation — and therefore the maximum allowed treasury transfer amount — is computed from an arbitrarily old price.

### Finding Description

In `rs/sns/governance/token_valuation/src/lib.rs`, the production `CmcBased30DayMovingAverageXdrsPerIcpClient` calls `get_average_icp_xdr_conversion_rate` on the CMC and immediately uses `response.data.xdr_permyriad_per_icp` without inspecting `response.data.timestamp_seconds`:

```rust
let xdr_per_icp =
    Decimal::from(response.data.xdr_permyriad_per_icp) * *UNITS_PER_PERMYRIAD;
Ok(xdr_per_icp)
``` [1](#0-0) 

The `IcpXdrConversionRateCertifiedResponse` carries a `timestamp_seconds` field in its `data`, but it is silently discarded. The shared `validate_exchange_rate` function only checks minimum source counts (ICP and CXDR), not rate freshness: [2](#0-1) 

The CMC's default initial rate is from May 2021 (`DEFAULT_ICP_XDR_CONVERSION_RATE_TIMESTAMP_SECONDS = 1620633600`). If the XRC integration is disabled (via a governance proposal with `DivergedRate` reason), the CMC serves this years-old rate indefinitely: [3](#0-2) 

The stale rate flows directly into `try_get_sns_token_balance_valuation`, which feeds `ProposalsAmountTotalUpperBound` — the on-chain limit governing how many tokens an SNS proposal can transfer from the treasury: [4](#0-3) 

### Impact Explanation

`ProposalsAmountTotalUpperBound` uses the XDR valuation of the SNS treasury to decide whether a `TransferSnsTreasuryFunds` proposal is admissible. A stale-high rate (ICP was worth more in the past) inflates the treasury valuation, making the limit more restrictive and potentially blocking legitimate transfers. A stale-low rate (but above the `MIN_XDRS_PER_ICP = 1` floor) deflates the valuation, allowing larger transfers than the current market value of the treasury warrants. In either case, the governance safety bound is computed from incorrect data. [5](#0-4) 

### Likelihood Explanation

The CMC heartbeat updates the rate every five minutes from the XRC. However, the XRC integration can be disabled by an NNS governance proposal (`DivergedRate` reason), after which the CMC serves its last stored rate (or the 2021 default) indefinitely. Additionally, if the XRC canister is unavailable for an extended period, the CMC's rate ages without any maximum-age guard at the consumer side. Any SNS governance participant submitting a `TransferSnsTreasuryFunds` proposal during such a window triggers the stale-rate path.

### Recommendation

In `CmcBased30DayMovingAverageXdrsPerIcpClient::get()`, after receiving the response, compare `response.data.timestamp_seconds` against the current canister time and reject (return `ValuationError`) if the rate is older than an acceptable threshold (e.g., 48 hours). Similarly, extend `validate_exchange_rate` or add a separate `validate_exchange_rate_freshness(rate, now, max_age)` helper that is called at every consumption site.

### Proof of Concept

1. NNS governance passes a proposal with `DivergedRate` reason, disabling the CMC's XRC integration.
2. The CMC now serves its last stored rate (potentially years old) from `get_average_icp_xdr_conversion_rate`.
3. An SNS neuron submits a `TransferSnsTreasuryFunds` proposal.
4. SNS governance calls `try_get_sns_token_balance_valuation` → `new_standard_xdrs_per_icp_client::get()` → CMC returns the stale rate.
5. `ProposalsAmountTotalUpperBound::in_tokens` computes the limit using the stale XDR/ICP value.
6. The proposal is accepted or rejected based on an incorrect treasury valuation, not the current market rate. [1](#0-0) [6](#0-5)

### Citations

**File:** rs/sns/governance/token_valuation/src/lib.rs (L37-57)
```rust
pub async fn try_get_sns_token_balance_valuation(
    account: Account,
    sns_ledger_canister_id: CanisterId,
    swap_canister_id: CanisterId,
) -> Result<Valuation, ValuationError> {
    let timestamp = now();

    try_get_balance_valuation_factors(
        account,
        &mut LedgerCanister::<CdkRuntime>::new(sns_ledger_canister_id),
        &mut IcpsPerSnsTokenClient::<CdkRuntime>::new(swap_canister_id, sns_ledger_canister_id),
        &mut new_standard_xdrs_per_icp_client::<CdkRuntime>(),
    )
    .await
    .map(|valuation_factors| Valuation {
        token: Token::SnsToken,
        account,
        timestamp,
        valuation_factors,
    })
}
```

**File:** rs/sns/governance/token_valuation/src/lib.rs (L435-458)
```rust
        async fn get(&mut self) -> Result<Decimal, ValuationError> {
            let (response,): (IcpXdrConversionRateCertifiedResponse,) =
                MyRuntime::call_with_cleanup(
                    CYCLES_MINTING_CANISTER_ID,
                    // This is not in the cmc.did file (yet).
                    "get_average_icp_xdr_conversion_rate",
                    ((),),
                )
                .await
                .map_err(|err| {
                    ValuationError::new_external(format!(
                        "Unable to determine XDRs per ICP, because the cycles minting canister \
                         did not reply to a get_average_icp_xdr_conversion_rate call: {err:?}",
                    ))
                })?;

            // No need to validate the cerificate in response, because query is not used in this
            // case (specifically, canister A in subnet X is calling (another) canister B in
            // (another) subnet Y).

            let xdr_per_icp =
                Decimal::from(response.data.xdr_permyriad_per_icp) * *UNITS_PER_PERMYRIAD;

            Ok(xdr_per_icp)
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

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L259-275)
```rust
        match call_xrc_result {
            Ok(exchange_rate) => {
                validate_exchange_rate(&exchange_rate)
                    .map_err(|error| UpdateExchangeRateError::InvalidRate(error.to_string()))?;
                let icp_xdr_conversion_rate = IcpXdrConversionRate::from(exchange_rate);
                if let Err(error) =
                    do_set_icp_xdr_conversion_rate(safe_state, env, icp_xdr_conversion_rate)
                {
                    return Err(UpdateExchangeRateError::FailedToSetRate(error));
                }
            }
            Err(error) => {
                return Err(UpdateExchangeRateError::FailedToRetrieveRate(
                    error.to_string(),
                ));
            }
        };
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L311-315)
```rust
                UpdateIcpXdrConversionRatePayloadReason::DivergedRate => {
                    state
                        .update_exchange_rate_canister_state
                        .replace(UpdateExchangeRateState::Disabled);
                }
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L34-65)
```rust
impl ProposalsAmountTotalUpperBound {
    // A treasury can be small, medium, or large. These are the boundaries between those regimes.
    const MAX_SMALL_TREASURY_SIZE_XDR: Decimal = dec!(100_000);
    const MAX_MEDIUM_TREASURY_SIZE_XDR: Decimal = dec!(1_200_000);

    // No matter how large the treasury is, not more than this amount can be removed (within a 7 day
    // window).
    const MAX_XDR: Decimal = dec!(300_000);

    /// A price quote less than this is considered "unrealistically" low. When that happens, we use
    /// this instead of the quoted value.
    ///
    /// # Motivation
    ///
    /// Low XDRs per ICP quotes would tend to cause our valuations to be in the "small" regime,
    /// where an SNS is allowed to take the biggest actions relative to their size. This is to
    /// minmize the damage caused by wacky price quotes.
    ///
    /// # What Value to Use
    ///
    /// Currently, the minimum XDRs per ICP used by NNS governance is 1. This is simply copied from
    /// there, specifically from the minimum_icp_xdr_rate field in NetworkEconomics.
    ///
    /// As of Mar 2024, the price of ICP is around 10 XDR. The lowest it has ever been is around 2.2
    /// XDR. FWIW, this is less than that.
    ///
    /// # Why Not Also Define MAX?
    ///
    /// Currently, we do not have/enforce a MAX_XDRS_PER_ICP, because this would tend to cause our
    /// valuations to be in the "large" regime, where actions are more limited.
    const MIN_XDRS_PER_ICP: Decimal = dec!(1);

```
