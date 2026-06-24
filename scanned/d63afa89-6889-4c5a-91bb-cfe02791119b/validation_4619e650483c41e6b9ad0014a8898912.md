### Title
Missing Price Staleness Validation in `CmcBased30DayMovingAverageXdrsPerIcpClient` Used for SNS Treasury Valuation - (File: rs/sns/governance/token_valuation/src/lib.rs)

### Summary

The `CmcBased30DayMovingAverageXdrsPerIcpClient::get()` function fetches the ICP/XDR conversion rate from the Cycles Minting Canister (CMC) to value SNS treasury assets, but never checks whether the returned rate's `timestamp_seconds` is fresh. A stale rate (e.g., from a CMC that has not received a fresh XRC update for days) is silently accepted and used to compute the treasury valuation that gates `TransferSnsTreasuryFunds` and `MintSnsTokens` proposals.

### Finding Description

`CmcBased30DayMovingAverageXdrsPerIcpClient::get()` calls `get_average_icp_xdr_conversion_rate` on the CMC and extracts only `xdr_permyriad_per_icp`, discarding `timestamp_seconds` entirely:

```rust
let xdr_per_icp =
    Decimal::from(response.data.xdr_permyriad_per_icp) * *UNITS_PER_PERMYRIAD;

Ok(xdr_per_icp)
``` [1](#0-0) 

The response type `IcpXdrConversionRateCertifiedResponse` carries a `timestamp_seconds` field that indicates when the CMC last successfully updated its average rate from the XRC. This timestamp is never compared against the current canister time, so there is no bound on how old the rate can be.

The CMC's `get_average_icp_xdr_conversion_rate` simply returns whatever `average_icp_xdr_conversion_rate` is stored in state, which is only updated when the CMC heartbeat successfully calls the XRC: [2](#0-1) 

The CMC heartbeat updates the rate every 5 minutes when the XRC is reachable. If the XRC is persistently unavailable (or returns errors), the CMC's stored average rate can become arbitrarily stale. The CMC has no mechanism to reject or flag its own stored rate as stale when serving `get_average_icp_xdr_conversion_rate`.

This stale rate then flows into `try_get_balance_valuation_factors`, which computes the XDR valuation of the SNS treasury: [3](#0-2) 

The resulting `Valuation` is stored as `ActionAuxiliary` at proposal submission time and reused at execution time to enforce the 7-day treasury transfer cap: [4](#0-3) 

Contrast this with the CMC's own internal staleness guard (`REFRESH_RATE_INTERVAL_SECONDS = 5 minutes`) that prevents the CMC from re-fetching too frequently, and the NNS Governance's `should_refresh_xdr_rate()` which checks that the locally cached rate is not older than one day before trusting it: [5](#0-4) 

No equivalent freshness check exists in the SNS token valuation path.

### Impact Explanation

The `xdrs_per_icp` rate is the primary driver of whether an SNS treasury is classified as "small" (≤100,000 XDR), "medium" (≤1,200,000 XDR), or "large" (>1,200,000 XDR), which determines the fraction of the treasury that can be transferred in a 7-day window: [6](#0-5) 

If the CMC's stored rate is stale and reflects a historically low ICP price (e.g., from a period of market stress), the treasury valuation will be understated, placing the SNS in the "small" regime where **100% of the treasury** can be transferred in a single 7-day window (`NoLimit` branch). This allows a governance-majority attacker to drain the entire SNS treasury in one proposal that passes the on-chain limit check, even if the real-time ICP price would have placed the treasury in the "large" regime with a hard cap of 300,000 XDR.

Conversely, a stale high rate inflates the valuation, placing the treasury in the "large" regime and blocking legitimate treasury proposals.

**Impact: Medium** — Incorrect treasury transfer limits; potential full treasury drain if stale-low rate coincides with a governance attack.

### Likelihood Explanation

The XRC is an external dependency that can fail or return errors for extended periods (the CMC already handles `StablecoinRateTooFewRates`, `CryptoBaseAssetNotFound`, etc. as transient errors). During such periods the CMC's stored average rate ages without update. The SNS token valuation code makes no attempt to detect this condition. Any SNS governance participant can submit a `TransferSnsTreasuryFunds` proposal during such a window; the stale rate is used without warning.

**Likelihood: Medium** — Requires XRC unavailability (a known operational scenario) coinciding with a governance proposal submission.

### Recommendation

In `CmcBased30DayMovingAverageXdrsPerIcpClient::get()`, after receiving the CMC response, compare `response.data.timestamp_seconds` against the current canister time. Reject (return `ValuationError::new_external`) if the rate is older than an acceptable threshold (e.g., 25–48 hours, consistent with the CMC's own `NUM_DAYS_FOR_ICP_XDR_AVERAGE` window and the NNS Governance's one-day staleness threshold):

```rust
let now_seconds = ic_cdk::api::time() / 1_000_000_000;
let rate_age_seconds = now_seconds.saturating_sub(response.data.timestamp_seconds);
const MAX_RATE_AGE_SECONDS: u64 = 2 * 24 * 3600; // 48 hours
if rate_age_seconds > MAX_RATE_AGE_SECONDS {
    return Err(ValuationError::new_external(format!(
        "ICP/XDR rate from CMC is stale: age {}s exceeds {}s",
        rate_age_seconds, MAX_RATE_AGE_SECONDS,
    )));
}
``` [7](#0-6) 

### Proof of Concept

1. The XRC canister becomes unavailable (returns `StablecoinRateTooFewRates` or similar) for >48 hours. The CMC heartbeat fails to update `average_icp_xdr_conversion_rate`; its stored rate reflects the ICP price from 2+ days ago.

2. An SNS governance participant submits a `TransferSnsTreasuryFunds` proposal. `validate_and_render_transfer_sns_treasury_funds` calls `treasury_valuation_if_proposal_amount_is_small_enough_or_err`. [8](#0-7) 

3. `assess_treasury_balance` → `Token::assess_balance` → `try_get_icp_balance_valuation` → `try_get_balance_valuation_factors` calls `CmcBased30DayMovingAverageXdrsPerIcpClient::get()`. [9](#0-8) 

4. The client calls `get_average_icp_xdr_conversion_rate` on the CMC and receives a stale rate (e.g., 1 XDR/ICP from a crash 3 days ago). No staleness check is performed; the rate is accepted. [7](#0-6) 

5. With `xdrs_per_icp = 1` (below `MIN_XDRS_PER_ICP` floor, so clamped to 1), a treasury of 1,000,000 ICP is valued at 1,000,000 XDR — placing it in the "large" regime. However, if the stale rate is, say, 0.5 XDR/ICP (below the floor), the floor clamps it to 1 XDR/ICP. If the stale rate is 2 XDR/ICP (above the floor but below the real ~10 XDR/ICP), the treasury is valued at 2,000,000 XDR — still "large" but with a lower cap than reality. Conversely, if the stale rate is 0.001 XDR/ICP (below floor, clamped to 1), a 50,000 ICP treasury is valued at 50,000 XDR — "small" regime, `NoLimit`, allowing full drain.

6. The stale-rate-based `Valuation` is stored in `ActionAuxiliary` and reused at execution time without re-fetching, locking in the incorrect limit for the proposal's lifetime. [10](#0-9)

### Citations

**File:** rs/sns/governance/token_valuation/src/lib.rs (L19-35)
```rust
pub async fn try_get_icp_balance_valuation(account: Account) -> Result<Valuation, ValuationError> {
    let timestamp = now();

    try_get_balance_valuation_factors(
        account,
        &mut LedgerCanister::<CdkRuntime>::new(ICP_LEDGER_CANISTER_ID),
        &mut IcpsPerIcpClient {},
        &mut new_standard_xdrs_per_icp_client::<CdkRuntime>(),
    )
    .await
    .map(|valuation_factors| Valuation {
        token: Token::Icp,
        account,
        timestamp,
        valuation_factors,
    })
}
```

**File:** rs/sns/governance/token_valuation/src/lib.rs (L141-191)
```rust
async fn try_get_balance_valuation_factors(
    account: Account,
    icrc1_client: &mut dyn Icrc1Client,
    icps_per_token_client: &mut dyn IcpsPerTokenClient,
    xdrs_per_icp_client: &mut dyn XdrsPerIcpClient,
) -> Result<ValuationFactors, ValuationError> {
    // Fetch the three ingredients:
    //
    //     1. balance
    //     2. token -> ICP
    //     3. ICP -> XDR
    //
    // No await here. Instead, we use join (right after this).
    let balance_of_request = icrc1_client.icrc1_balance_of(account);
    let icps_per_token_request = icps_per_token_client.get();
    let xdrs_per_icp_request = xdrs_per_icp_client.get();

    // Make all (3) requests (concurrently).
    let (balance_of_response, icps_per_token_response, xdrs_per_icp_response) = join!(
        balance_of_request,
        icps_per_token_request,
        xdrs_per_icp_request,
    );

    // Unwrap/forward errors to the caller.
    let balance_of_response = balance_of_response.map_err(|err| {
        ValuationError::new_external(format!("Unable to obtain balance from ledger: {err:?}"))
    })?;
    let icps_per_token_response = icps_per_token_response.map_err(|err| {
        ValuationError::new_external(format!("Unable to determine ICPs per token: {err:?}"))
    })?;
    let xdrs_per_icp_response = xdrs_per_icp_response.map_err(|err| {
        ValuationError::new_external(format!("Unable to obtain XDR per ICP: {err:?}"))
    })?;

    // Extract and interpret the data we actually care about from the (Ok) responses.
    let tokens = Decimal::from(u128::try_from(balance_of_response.0).map_err(|err| {
        ValuationError::new_arithmetic(format!(
            "Balance of {account:?} does not fit in u128: {err:?}"
        ))
    })?) / Decimal::from(E8);
    let icps_per_token = icps_per_token_response;
    let xdrs_per_icp = xdrs_per_icp_response;

    // Compose the fetched/interpretted data (i.e. multiply them) to construct the final result.
    Ok(ValuationFactors {
        tokens,
        icps_per_token,
        xdrs_per_icp,
    })
}
```

**File:** rs/sns/governance/token_valuation/src/lib.rs (L435-459)
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
        }
```

**File:** rs/nns/cmc/src/main.rs (L891-912)
```rust
#[query(hidden = true)]
fn get_average_icp_xdr_conversion_rate(_: ()) -> IcpXdrConversionRateCertifiedResponse {
    with_state(|state| {
        let witness_generator = convert_data_to_mixed_hash_tree(state);
        let average_icp_xdr_conversion_rate = state
            .average_icp_xdr_conversion_rate
            .as_ref()
            .expect("average_icp_xdr_conversion_rate is not set");

        let payload = convert_conversion_rate_to_payload(
            average_icp_xdr_conversion_rate,
            Label::from(LABEL_AVERAGE_ICP_XDR_CONVERSION_RATE),
            witness_generator,
        );

        IcpXdrConversionRateCertifiedResponse {
            data: average_icp_xdr_conversion_rate.clone(),
            hash_tree: payload,
            certificate: ic_cdk::api::data_certificate().unwrap_or_default(),
        }
    })
}
```

**File:** rs/sns/governance/src/proposal.rs (L554-578)
```rust
async fn validate_and_render_transfer_sns_treasury_funds(
    transfer: &TransferSnsTreasuryFunds,
    sns_transfer_fee_e8s: u64,
    env: &dyn Environment,
    swap_canister_id: CanisterId,
    sns_ledger_canister_id: CanisterId,
    proposals: impl Iterator<Item = &ProposalData>,
) -> Result<
    (
        String, // Rendering.
        ActionAuxiliary,
    ),
    String,
> {
    let mut defects = vec![];

    // Validate amount. This requires calling CMC and the swap canister; hence, await.
    let valuation = treasury_valuation_if_proposal_amount_is_small_enough_or_err(
        env,
        sns_ledger_canister_id,
        swap_canister_id,
        proposals,
        transfer,
    )
    .await;
```

**File:** rs/sns/governance/src/proposal.rs (L2600-2656)
```rust
pub(crate) fn transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err<'a>(
    transfer: &TransferSnsTreasuryFunds,
    valuation: Valuation,
    proposals: impl Iterator<Item = &'a ProposalData>,
    now_timestamp_seconds: u64,
) -> Result<(), GovernanceError> {
    let allowance_tokens = transfer_sns_treasury_funds_7_day_total_upper_bound_tokens(valuation)
        .map_err(|err| {
            // This should not be possible, because valuation was already used the same way during
            // proposal submission/creation/validation.
            GovernanceError::new_with_message(
                ErrorType::InconsistentInternalData,
                format!(
                    "Unable to determined upper bound on the amount of \
                     TransferSnsTreasuryFunds proposals: {err:?}\nvaluation:{valuation:?}",
                ),
            )
        })?;

    // The total calculated here _could_ be different from what was calculated at proposal
    // submission/creation time. A difference would result from the execution of (another)
    // TransferSnsTreasuryFunds proposal between now and then.
    let spent_tokens = total_treasury_transfer_amount_tokens(
        proposals,
        transfer.from_treasury(),
        now_timestamp_seconds - 7 * ONE_DAY_SECONDS,
    )
    .map_err(|message| {
        GovernanceError::new_with_message(ErrorType::InconsistentInternalData, message)
    })?;

    let remainder_tokens = allowance_tokens - spent_tokens;
    let transfer_amount_tokens = denominations_to_tokens(transfer.amount_e8s, E8)
        // This Err cannot be provoked, because we are dividing a u64 (amount_e8s) by a positive
        // integer (E8).
        .ok_or_else(|| {
            GovernanceError::new_with_message(
                ErrorType::UnreachableCode,
                format!(
                    "Unable to convert proposals amount {} e8s to tokens.",
                    transfer.amount_e8s,
                ),
            )
        })?;
    if transfer_amount_tokens > remainder_tokens {
        return Err(GovernanceError::new_with_message(
            ErrorType::PreconditionFailed,
            format!(
                "Executing this proposal is not allowed at this time, because doing \
                 so would cause the 7 day upper bound of {allowance_tokens} tokens to be exceeded. \
                 Maybe, try again later? The total amount transferred in the past \
                 7 days stands at {spent_tokens} tokens, and the amount in this proposal is {transfer_amount_tokens} \
                 tokens. The upper bound is based on treasury valuation factors at \
                 the time of proposal submission: {valuation:?}",
            ),
        ));
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

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L34-64)
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

**File:** rs/sns/governance/src/governance.rs (L3000-3005)
```rust
        transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err(
            transfer,
            valuation?,
            self.proto.proposals.values(),
            self.env.now(),
        )?;
```
