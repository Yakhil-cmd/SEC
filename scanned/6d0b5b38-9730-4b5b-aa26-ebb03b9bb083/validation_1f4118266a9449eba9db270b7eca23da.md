### Title
Unvalidated Timestamp on ICP/XDR Rate Returned from CMC Used to Gate SNS Treasury Transfers - (File: rs/sns/governance/token_valuation/src/lib.rs)

### Summary
The `CmcBased30DayMovingAverageXdrsPerIcpClient` in `rs/sns/governance/token_valuation/src/lib.rs` fetches the ICP/XDR conversion rate from the Cycles Minting Canister (CMC) and uses it directly to compute SNS treasury valuations — without validating the `timestamp_seconds` of the returned rate. If the CMC's rate is stale (e.g., the XRC canister has been unresponsive for an extended period), the SNS governance canister will use an outdated price to enforce treasury transfer and token minting limits, potentially allowing transfers that should be blocked, or blocking transfers that should be allowed.

### Finding Description
In `rs/sns/governance/token_valuation/src/lib.rs`, the function `new_standard_xdrs_per_icp_client` implements `CmcBased30DayMovingAverageXdrsPerIcpClient`, which calls `get_average_icp_xdr_conversion_rate` on the CMC:

```rust
async fn get(&mut self) -> Result<Decimal, ValuationError> {
    let (response,): (IcpXdrConversionRateCertifiedResponse,) =
        MyRuntime::call_with_cleanup(
            CYCLES_MINTING_CANISTER_ID,
            "get_average_icp_xdr_conversion_rate",
            ((),),
        )
        .await
        ...?;

    // No need to validate the cerificate in response, because query is not used in this
    // case (specifically, canister A in subnet X is calling (another) canister B in
    // (another) subnet Y).

    let xdr_per_icp =
        Decimal::from(response.data.xdr_permyriad_per_icp) * *UNITS_PER_PERMYRIAD;

    Ok(xdr_per_icp)
}
```

The code explicitly skips certificate validation (justified by the inter-canister call context), but it also performs **no validation of `response.data.timestamp_seconds`**. There is no check that the returned rate is recent enough to be trusted.

The CMC's `average_icp_xdr_conversion_rate` is only updated when the CMC successfully polls the XRC canister. The CMC's own `do_set_icp_xdr_conversion_rate` only checks that the new rate's timestamp is strictly greater than the current one — it does not enforce a maximum age. If the XRC canister becomes unavailable (e.g., due to XRC canister issues, rate limiting, or insufficient cycles), the CMC will retain its last known rate indefinitely. The CMC's `REFRESH_RATE_INTERVAL_SECONDS` is 5 minutes, but there is no upper bound on how stale the stored rate can become.

This stale rate is then consumed by `try_get_icp_balance_valuation` and `try_get_sns_token_balance_valuation`, which feed into `treasury_valuation_if_proposal_amount_is_small_enough_or_err` in `rs/sns/governance/src/proposal.rs`. This function gates `TransferSnsTreasuryFunds` and `MintSnsTokens` proposals. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

### Impact Explanation
The `xdrs_per_icp` value is a direct multiplier in the treasury valuation formula: `tokens * icps_per_token * xdrs_per_icp`. A stale rate that is significantly lower than the current market price causes the treasury to appear smaller in XDR terms, placing it in a lower tier of the `ProposalsAmountTotalUpperBound` regime. This allows proportionally larger treasury transfers or token mints than the governance rules intend. Conversely, a stale rate that is higher than the current market price would over-restrict legitimate proposals.

The impact is governance authorization bypass: SNS treasury transfer limits are enforced using a price that may be days or weeks old, undermining the economic safety guarantees of the SNS governance system. [5](#0-4) [6](#0-5) 

### Likelihood Explanation
The CMC polls the XRC every 5 minutes under normal conditions. However, the XRC canister can return errors (e.g., `StablecoinRateTooFewRates`, `RateLimited`, `NotEnoughCycles`), and the CMC retries only once per minute on failure. There is no maximum staleness enforced anywhere in the chain. A sustained XRC outage of any duration leaves the CMC's rate frozen. The SNS governance canister has no independent mechanism to detect or reject a stale rate. This is a realistic scenario given the XRC's dependency on external HTTP outcalls to forex and crypto exchanges. [7](#0-6) [8](#0-7) 

### Recommendation
In `CmcBased30DayMovingAverageXdrsPerIcpClient::get`, after receiving the response, validate that `response.data.timestamp_seconds` is within an acceptable staleness window (e.g., no older than 48 hours relative to the current canister time). If the rate is too old, return a `ValuationError::new_external(...)` rather than proceeding with the stale value. This mirrors the pattern already used in NNS Governance's `should_refresh_xdr_rate`, which checks that the rate is not older than `ONE_DAY_SECONDS`. [9](#0-8) [1](#0-0) 

### Proof of Concept
1. The XRC canister becomes unavailable (e.g., returns `StablecoinRateTooFewRates` persistently). The CMC stops updating its `average_icp_xdr_conversion_rate`, which remains frozen at the last successful value.
2. ICP market price drops significantly (e.g., from 10 XDR to 2 XDR), but the CMC still reports 10 XDR/ICP.
3. An SNS governance proposal for `TransferSnsTreasuryFunds` is submitted. `treasury_valuation_if_proposal_amount_is_small_enough_or_err` calls `assess_treasury_balance`, which calls `new_standard_xdrs_per_icp_client::get()`.
4. The client calls `get_average_icp_xdr_conversion_rate` on the CMC and receives the stale 10 XDR/ICP rate. No timestamp check is performed.
5. The treasury is valued at 5× its actual current XDR value. The `ProposalsAmountTotalUpperBound` places it in a higher tier, allowing a transfer of up to `MAX_XDR = 300,000 XDR` worth of tokens — but at the stale price, this corresponds to far more tokens than the governance rules intend to permit at the real current price.
6. The proposal passes validation and executes, draining more treasury value than the SNS governance rules were designed to allow. [1](#0-0) [10](#0-9) [11](#0-10)

### Citations

**File:** rs/sns/governance/token_valuation/src/lib.rs (L117-127)
```rust
impl ValuationFactors {
    pub fn to_xdr(&self) -> Decimal {
        let Self {
            tokens,
            icps_per_token,
            xdrs_per_icp,
        } = self;

        tokens * icps_per_token * xdrs_per_icp
    }
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

**File:** rs/sns/governance/src/proposal.rs (L770-816)
```rust
async fn treasury_valuation_if_proposal_amount_is_small_enough_or_err<MyTokenProposalAction>(
    env: &dyn Environment,
    sns_ledger_canister_id: CanisterId,
    swap_canister_id: CanisterId,
    proposals: impl Iterator<Item = &ProposalData>,
    action: &MyTokenProposalAction,
) -> Result<Valuation, String>
where
    MyTokenProposalAction: TokenProposalAction,
{
    let spent_tokens = action.recent_amount_total_tokens(proposals, env.now())?;

    // Get valuation of the tokens in the treasury.
    let token = action.token()?;
    let valuation = assess_treasury_balance(
        token,
        env.canister_id(),
        sns_ledger_canister_id,
        swap_canister_id,
    )
    .await?;

    // From valuation, determine limit on the total from the past 7 days.
    let max_tokens = MyTokenProposalAction::recent_amount_total_upper_bound_tokens(&valuation)
        // Err is most likely a bug.
        .map_err(|treasury_limit_error| {
            format!("Unable to validate amount: {treasury_limit_error:?}",)
        })?;

    // Finally, inspect the proposal's amount: it must not exceed max - spent (remainder). Or if
    // you prefer, equivalently, amount + spent must be <= max.
    let allowance_remainder_tokens = max_tokens.checked_sub(spent_tokens).ok_or_else(|| {
        format!("Arithmetic error while performing {max_tokens} - {spent_tokens}",)
    })?;
    let proposal_amount_tokens = action.proposal_amount_tokens()?;
    if proposal_amount_tokens > allowance_remainder_tokens {
        // Although it might not be obvious to the user, their proposal is invalid, and we
        // consider it to be "their fault".
        return Err(format!(
            "Amount is too large. Within the past 7 days, a total of {spent_tokens} tokens has already \
             been executed in like proposals. Whereas, at most {max_tokens} is allowed. An additional \
             {proposal_amount_tokens} tokens from this proposal would cause that upper bound to be exceeded. \
             Maybe, try again in a few days?"
        ));
    }

    Ok(valuation)
```

**File:** rs/nns/cmc/src/main.rs (L1008-1039)
```rust
/// canister's certified data
fn do_set_icp_xdr_conversion_rate(
    safe_state: &'static LocalKey<RefCell<Option<State>>>,
    env: &impl Environment,
    proposed_conversion_rate: IcpXdrConversionRate,
) -> Result<(), String> {
    print(format!(
        "[cycles] conversion rate update: {proposed_conversion_rate:?}"
    ));

    if proposed_conversion_rate.xdr_permyriad_per_icp == 0 {
        return Err("Proposed conversion rate must be greater than 0".to_string());
    }

    mutate_state(safe_state, |state| {
        if let Some(current_conversion_rate) = state.icp_xdr_conversion_rate.as_ref()
            && proposed_conversion_rate.timestamp_seconds
                <= current_conversion_rate.timestamp_seconds
        {
            return Err(
                "Proposed conversion rate must have greater timestamp than current one".to_string(),
            );
        }

        state.icp_xdr_conversion_rate = Some(proposed_conversion_rate.clone());
        update_recent_icp_xdr_rates(state, &proposed_conversion_rate);

        let witness_generator = convert_data_to_mixed_hash_tree(state);
        env.set_certified_data(&witness_generator.hash_tree().digest().0[..]);

        Ok(())
    })
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L15-16)
```rust
/// If the rate is older than this value, the CMC should ask for a new rate.
const REFRESH_RATE_INTERVAL_SECONDS: u64 = 5 * ONE_MINUTE_SECONDS;
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L86-165)
```rust
impl UpdateExchangeRateGuard {
    /// Set the calling status to active.
    fn new(
        safe_state: &'static LocalKey<RefCell<Option<State>>>,
        current_minute_in_seconds: u64,
    ) -> Result<Self, UpdateExchangeRateError> {
        let current_call_state = read_state(safe_state, |state| {
            state
                .update_exchange_rate_canister_state
                .unwrap_or_default()
        });

        if current_call_state == UpdateExchangeRateState::Disabled {
            return Err(UpdateExchangeRateError::Disabled);
        }

        if current_call_state == UpdateExchangeRateState::InProgress {
            return Err(UpdateExchangeRateError::UpdateAlreadyInProgress);
        }

        if let UpdateExchangeRateState::GetRateAt(next_attempt_seconds) = current_call_state
            && current_minute_in_seconds < next_attempt_seconds
        {
            return Err(UpdateExchangeRateError::NotReadyToGetRate(
                next_attempt_seconds,
            ));
        }

        mutate_state(safe_state, |state| {
            state
                .update_exchange_rate_canister_state
                .replace(UpdateExchangeRateState::InProgress);
        });

        Ok(Self {
            safe_state,
            current_minute_in_seconds,
        })
    }

    // This function helps schedule the next attempt at retrieving a rate from
    // the exchange rate canister. If the result of the in progress call is successful,
    // a new attempt to get the rate is schedule at the next five minute interval (:00, :05, :10, ...).
    // If the result has failed due to a failure receiving the rate or the rate was
    // determined to be invalid, a new attempt is schedule for the next minute.
    //
    // If the update cycle has been disabled, this function skips the scheduling.
    fn schedule_next_attempt(&self, result: &Result<(), UpdateExchangeRateError>) {
        mutate_state(self.safe_state, |state| {
            if let Some(UpdateExchangeRateState::Disabled) =
                state.update_exchange_rate_canister_state
            {
                return;
            }

            match result {
                Ok(_) => {
                    state.update_exchange_rate_canister_state.replace(
                        UpdateExchangeRateState::get_rate_at_next_refresh_rate_interval(
                            self.current_minute_in_seconds,
                        ),
                    );
                }
                Err(error) => match error {
                    UpdateExchangeRateError::UpdateAlreadyInProgress => {}
                    UpdateExchangeRateError::Disabled => {}
                    UpdateExchangeRateError::NotReadyToGetRate(_) => {}
                    UpdateExchangeRateError::FailedToRetrieveRate(_)
                    | UpdateExchangeRateError::FailedToSetRate(_)
                    | UpdateExchangeRateError::InvalidRate(_) => {
                        state.update_exchange_rate_canister_state.replace(
                            UpdateExchangeRateState::get_rate_at_next_minute(
                                self.current_minute_in_seconds,
                            ),
                        );
                    }
                },
            }
        });
    }
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L232-279)
```rust
/// The periodic task for collecting the ICP/XDR rate from the Exchange Rate Canister.
/// To avoid having multiple calls sent to the Exchange Rate Canister,
/// this function contains a guard to ensure multiple calls cannot be made until
/// the prior call is complete.
pub async fn update_exchange_rate(
    safe_state: &'static LocalKey<RefCell<Option<State>>>,
    env: &impl Environment,
    xrc_client: &impl ExchangeRateCanisterClient,
) -> Result<(), UpdateExchangeRateError> {
    let now_timestamp_seconds = env.now_timestamp_seconds();
    let current_minute_seconds =
        round_down_to_multiple_of(now_timestamp_seconds, ONE_MINUTE_SECONDS);

    UpdateExchangeRateGuard::with_guard(safe_state, current_minute_seconds, async {
        let call_xrc_result = xrc_client.get_icp_to_xdr_exchange_rate(None).await;
        // Check if updating the rate via the exchange rate canister was disabled while retrieving the rate.
        // If it has, exit early.
        let is_updating_rate_disabled = read_state(safe_state, |state| {
            state
                .update_exchange_rate_canister_state
                .unwrap_or_default()
                == UpdateExchangeRateState::Disabled
        });
        if is_updating_rate_disabled {
            return Err(UpdateExchangeRateError::Disabled);
        }

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

        Ok(())
    })
    .await
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L34-114)
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

    fn in_tokens(mut valuation: Valuation) -> Result<Decimal, ProposalsAmountTotalLimitError> {
        Self::clamp_xdrs_per_icp(&mut valuation);

        let ValuationFactors {
            tokens: balance_tokens,
            icps_per_token,
            xdrs_per_icp,
        } = valuation.valuation_factors;

        let self_ = Self::from_valuation_xdr(valuation.to_xdr());
        let result_tokens = match self_ {
            Self::NoLimit => balance_tokens,

            Self::Fraction(fraction) => balance_tokens
                .checked_mul(fraction)
                // Overflow should not be possible, since fraction is supposed to be at most 1.0.
                .ok_or_else(|| {
                    ProposalsAmountTotalLimitError::new_arithmetic(format!(
                        "Unable to perform {balance_tokens} * {fraction}.",
                    ))
                })?,

            Self::Xdr(max_xdr) => {
                let xdrs_per_token = xdrs_per_icp.checked_mul(icps_per_token).ok_or_else(|| {
                    ProposalsAmountTotalLimitError::new_arithmetic(format!(
                        "XDRs per token could not be calculated from valuation: {valuation:?}"
                    ))
                })?;

                // Calculate the inverse conversion rate.
                if xdrs_per_token == Decimal::from(0) {
                    // This is not reachable, because in this case, valuation.to_xdr() would return
                    // 0, and in that case, we would have taken the NoLimit branch.
                    return Err(ProposalsAmountTotalLimitError::new_arithmetic(format!(
                        "It appears that the tokens have zero value in XDR. valuation = {valuation:?}"
                    )));
                }
                let tokens_per_xdr = xdrs_per_token.inv();

                max_xdr.checked_mul(tokens_per_xdr).ok_or_else(|| {
                    ProposalsAmountTotalLimitError::new_arithmetic(format!(
                        "Max tokens could not be calculated with valuation: {valuation:?}",
                    ))
                })?
            }
        };

        Ok(result_tokens)
    }
```

**File:** rs/nns/governance/src/governance.rs (L6336-6348)
```rust
    fn should_refresh_xdr_rate(&self) -> bool {
        let xdr_conversion_rate = &self.heap_data.xdr_conversion_rate;

        let now_seconds = self.env.now();

        let seconds_since_last_conversion_rate_refresh =
            now_seconds.saturating_sub(xdr_conversion_rate.timestamp_seconds);

        // Return `true` if more than 1 day has passed since the last `xdr_conversion_rate` was
        // updated. This assumes that `xdr_conversion_rate.timestamp_seconds` is rounded down to
        // the nearest day's beginning.
        seconds_since_last_conversion_rate_refresh > ONE_DAY_SECONDS
    }
```
