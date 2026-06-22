### Title
Stale ICP/XDR Exchange Rate Consumed Without Freshness Verification in SNS Treasury Valuation - (File: rs/sns/governance/token_valuation/src/lib.rs)

### Summary
The `CmcBased30DayMovingAverageXdrsPerIcpClient` in `rs/sns/governance/token_valuation/src/lib.rs` fetches the ICP/XDR exchange rate from the Cycles Minting Canister (CMC) and uses it directly to compute SNS treasury valuations — which gate `TransferSnsTreasuryFunds` and `MintSnsTokens` proposals — without verifying the freshness of the returned rate's `timestamp_seconds`. If the CMC's exchange rate update pipeline stalls (e.g., the XRC canister becomes unreachable for an extended period), the SNS governance canister will silently use an arbitrarily old rate to enforce treasury transfer limits.

### Finding Description

The `new_standard_xdrs_per_icp_client` function constructs a `CmcBased30DayMovingAverageXdrsPerIcpClient` that calls `get_average_icp_xdr_conversion_rate` on the CMC and extracts `response.data.xdr_permyriad_per_icp` directly:

```rust
let xdr_per_icp =
    Decimal::from(response.data.xdr_permyriad_per_icp) * *UNITS_PER_PERMYRIAD;
Ok(xdr_per_icp)
``` [1](#0-0) 

The `response.data.timestamp_seconds` field is present in the returned `IcpXdrConversionRate` struct but is never inspected. No check is made to determine whether the rate is recent enough to be trusted. [2](#0-1) 

The CMC's `get_average_icp_xdr_conversion_rate` query handler simply returns whatever `average_icp_xdr_conversion_rate` is stored in state, with no staleness guard:

```rust
fn get_average_icp_xdr_conversion_rate(_: ()) -> IcpXdrConversionRateCertifiedResponse {
    with_state(|state| {
        ...
        IcpXdrConversionRateCertifiedResponse {
            data: average_icp_xdr_conversion_rate.clone(),
            ...
        }
    })
}
``` [3](#0-2) 

The CMC does have a heartbeat-driven refresh mechanism (`update_exchange_rate`) that polls the XRC every 5 minutes. However, if the XRC canister is unavailable for an extended period, the CMC simply retains the last successfully fetched rate indefinitely. The `REFRESH_RATE_INTERVAL_SECONDS` constant controls polling cadence but does not impose a maximum age on the stored rate: [4](#0-3) 

The valuation computed from this potentially stale rate is then used at both proposal submission time and execution time to enforce the 7-day treasury transfer upper bound for `TransferSnsTreasuryFunds` and `MintSnsTokens` proposals: [5](#0-4) [6](#0-5) 

### Impact Explanation

The treasury transfer limit for SNS proposals is computed as a fraction of the treasury's XDR value. If the stored ICP/XDR rate is stale and significantly lower than the current market rate (e.g., ICP price has risen sharply since the last successful XRC fetch), the computed treasury value in XDR will be understated, making the limit more restrictive than intended — potentially blocking legitimate governance proposals. Conversely, if the rate is stale and higher than the current market rate (ICP price has fallen), the limit will be overstated, allowing larger transfers than the protocol intends to permit. The latter case is the more security-relevant direction: an SNS governance community could execute treasury transfers that exceed the intended 7-day cap denominated in current XDR value.

**Impact: Medium** — Incorrect treasury transfer limits on SNS `TransferSnsTreasuryFunds` and `MintSnsTokens` proposals. In the worst case (stale high rate), the protocol's financial guardrails are weakened, allowing larger-than-intended treasury outflows.

### Likelihood Explanation

**Likelihood: Low-Medium** — The CMC's XRC polling runs every 5 minutes under normal conditions. A stale rate requires the XRC canister to be unreachable for a sustained period. However, the XRC canister is a separate canister on the IC and can experience downtime. There is no maximum-age enforcement anywhere in the pipeline between the XRC fetch and the SNS valuation consumer. The code explicitly notes the absence of certificate validation in the inter-canister path: [7](#0-6) 

### Recommendation

In `CmcBased30DayMovingAverageXdrsPerIcpClient::get`, after receiving the response, compare `response.data.timestamp_seconds` against the current canister time. If the rate is older than a defined freshness threshold (e.g., 24–48 hours), return a `ValuationError` rather than proceeding with the stale rate. A suitable threshold should be chosen to tolerate brief XRC outages while still bounding the maximum staleness used in financial decisions.

```rust
let age_seconds = now_seconds().saturating_sub(response.data.timestamp_seconds);
const MAX_RATE_AGE_SECONDS: u64 = 48 * 3600;
if age_seconds > MAX_RATE_AGE_SECONDS {
    return Err(ValuationError::new_external(format!(
        "ICP/XDR rate is too stale: {} seconds old", age_seconds
    )));
}
```

### Proof of Concept

1. The XRC canister becomes unreachable for >48 hours (e.g., due to a subnet issue or canister upgrade).
2. The CMC's `update_exchange_rate` heartbeat repeatedly fails; the stored `average_icp_xdr_conversion_rate` retains its last value from days ago.
3. An SNS governance proposal for `TransferSnsTreasuryFunds` is submitted. `validate_and_render_transfer_sns_treasury_funds` calls `treasury_valuation_if_proposal_amount_is_small_enough_or_err`, which calls `try_get_icp_balance_valuation` or `try_get_sns_token_balance_valuation`.
4. `new_standard_xdrs_per_icp_client` calls `get_average_icp_xdr_conversion_rate` on the CMC and receives the stale rate.
5. The stale rate is used without any age check to compute the treasury's XDR value and the 7-day transfer upper bound.
6. If ICP price has fallen since the stale rate was recorded, the computed XDR limit is inflated, allowing a transfer that exceeds the intended current-value cap. [8](#0-7) [9](#0-8) [10](#0-9)

### Citations

**File:** rs/sns/governance/token_valuation/src/lib.rs (L19-57)
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

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L15-16)
```rust
/// If the rate is older than this value, the CMC should ask for a new rate.
const REFRESH_RATE_INTERVAL_SECONDS: u64 = 5 * ONE_MINUTE_SECONDS;
```

**File:** rs/sns/governance/src/proposal.rs (L551-578)
```rust
/// Validates and render TransferSnsTreasuryFunds proposal
///
/// Returns ActionAuxiliary::TransferSnsTreasuryFunds.
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

**File:** rs/sns/governance/src/proposal.rs (L875-930)
```rust
async fn validate_and_render_mint_sns_tokens(
    mint_sns_tokens: &MintSnsTokens,
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

    // Validate amount. (This requires calling CMC and the swap canister; hence, await.)
    let valuation = treasury_valuation_if_proposal_amount_is_small_enough_or_err(
        env,
        sns_ledger_canister_id,
        swap_canister_id,
        proposals,
        mint_sns_tokens,
    )
    .await;
    let valuation = match valuation {
        Ok(ok) => Some(ok),
        Err(err) => {
            defects.push(err);
            None
        }
    };

    locally_validate_and_render_mint_sns_tokens(mint_sns_tokens, sns_transfer_fee_e8s, defects)
        .and_then(|rendering| {
            match valuation {
                Some(valuation) => Ok((rendering, ActionAuxiliary::MintSnsTokens(valuation))),

                // Proof that this never happens:
                //
                //   1. valuation = None means that amount_result was Err.
                //
                //   2. In that case, nonempty defects was passed to
                //      locally_validate_and_render_mint_sns_tokens.
                //
                //   3. In that case, the function always returns Err.
                //
                //   4. Then, this closure doesn't get called.
                None => Err(
                    "There is a bug in the amount validator. Somehow, no valuation, \
                     even though a rendering was generated."
                        .to_string(),
                ),
            }
        })
}
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
