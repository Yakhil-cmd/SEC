### Title
Missing Lower-Bound Clamp on `icps_per_token` in SNS Treasury Valuation Allows Artificially Low Token Price to Bypass Withdrawal Limits - (File: rs/sns/governance/token_valuation/src/lib.rs)

### Summary

The SNS governance canister computes a treasury valuation to enforce 7-day withdrawal limits on `TransferSnsTreasuryFunds` and `MintSnsTokens` proposals. The valuation multiplies three factors: `tokens × icps_per_token × xdrs_per_icp`. While `xdrs_per_icp` is clamped to a minimum of `1` XDR/ICP, the `icps_per_token` factor — derived from the SNS swap canister's `get_derived_state` response — has **no analogous minimum clamp**. An attacker who can influence the swap canister's `sns_tokens_per_icp` field (which is a live, manipulable ratio computed from current participation) can cause `icps_per_token` to be computed as near-zero, collapsing the treasury's XDR valuation below 100,000 XDR, which triggers the `NoLimit` branch and removes all withdrawal caps.

### Finding Description

The `IcpsPerSnsTokenClient::fetch_icps_per_sns_token` function in `rs/sns/governance/token_valuation/src/lib.rs` computes the SNS token price in ICP by:

1. Calling `get_derived_state` on the SNS swap canister to obtain `sns_tokens_per_icp` (a live f32 ratio).
2. Inverting it to get `initial_icps_per_sns_token`.
3. Dividing by `total_inflation` (current supply / initial supply). [1](#0-0) 

The `sns_tokens_per_icp` field in the swap canister's `DerivedState` is computed as:

```
sns_tokens_per_icp = tokens_available_for_swap / participant_total_icp_e8s
``` [2](#0-1) 

This is a **live spot ratio** based on current ICP participation. If `participant_total_icp_e8s` is very large (e.g., because the swap is still open and an attacker has contributed a large amount of ICP), `sns_tokens_per_icp` becomes very large, and its inverse `icps_per_token` becomes near-zero.

The treasury limit logic in `ProposalsAmountTotalUpperBound::in_tokens` applies `clamp_xdrs_per_icp` to enforce `MIN_XDRS_PER_ICP = 1`, but there is **no equivalent `clamp_icps_per_token`**: [3](#0-2) 

The comment in the code explicitly acknowledges the asymmetry:

> "Currently, we do not have/enforce a MAX_XDRS_PER_ICP, because this would tend to cause our valuations to be in the 'large' regime, where actions are more limited." [4](#0-3) 

But the symmetric risk — an artificially **low** `icps_per_token` — is not addressed. A near-zero `icps_per_token` collapses `valuation.to_xdr()` below `MAX_SMALL_TREASURY_SIZE_XDR` (100,000 XDR), causing `from_valuation_xdr` to return `NoLimit`: [5](#0-4) 

When `NoLimit` is returned, `in_tokens` returns `balance_tokens` — the **entire treasury balance** — as the allowed withdrawal amount, bypassing all caps. [6](#0-5) 

### Impact Explanation

An SNS with a large treasury (e.g., worth >100,000 XDR) is protected by the 7-day withdrawal cap (at most 25% of treasury for medium, or 300,000 XDR for large). If `icps_per_token` is manipulated to near-zero at proposal submission time, the valuation falls into the `NoLimit` regime, and the SNS governance canister will allow a `TransferSnsTreasuryFunds` or `MintSnsTokens` proposal to drain the entire treasury in a single 7-day window. The valuation is **locked in at proposal submission time** and reused at execution time: [7](#0-6) [8](#0-7) 

This is a **governance authorization / ledger conservation bug**: an SNS token holder with sufficient voting power can submit a proposal during a window when `icps_per_token` is artificially low, get it adopted, and drain the treasury beyond the intended cap.

### Likelihood Explanation

The SNS swap canister's `get_derived_state` is a **query call** returning a live spot ratio. The `sns_tokens_per_icp` value is computed from `tokens_available_for_swap / participant_total_icp_e8s`. During an open swap, any participant who contributes a large amount of ICP can transiently inflate this ratio. After the swap is committed/finalized, the `sns_tokens_per_icp` reflects the final swap price, which is fixed — but the swap canister remains deployed and queryable indefinitely. If the SNS token has very low market value relative to ICP (i.e., many SNS tokens per ICP), the `icps_per_token` will naturally be very small, and no manipulation is needed. For SNS tokens with low ICP value, this condition can arise organically, not just through active manipulation.

### Recommendation

Apply a minimum clamp to `icps_per_token` analogous to `MIN_XDRS_PER_ICP`, before computing the treasury valuation used for limit enforcement. Add a `MIN_ICPS_PER_TOKEN` constant (e.g., derived from the minimum ICP/token price observed at swap finalization, or a protocol-defined floor) and apply it in `clamp_xdrs_per_icp`'s counterpart. Alternatively, enforce a minimum XDR valuation per token directly, so that no single factor can collapse the total valuation to the `NoLimit` regime. [9](#0-8) 

### Proof of Concept

1. An SNS has a treasury worth 2,000,000 XDR (large regime; cap = 300,000 XDR).
2. The SNS token has low market value: 10,000 SNS tokens per ICP → `icps_per_token = 0.0001`.
3. `xdrs_per_icp` = 10 XDR/ICP (clamped to min 1, so 10 is used).
4. Treasury holds 1,000,000 SNS tokens.
5. `valuation.to_xdr()` = `1,000,000 × 0.0001 × 10` = **1,000 XDR** → falls into `NoLimit`.
6. `in_tokens` returns `balance_tokens = 1,000,000` SNS tokens as the allowed amount.
7. A whale neuron submits a `TransferSnsTreasuryFunds` proposal for 1,000,000 SNS tokens (the entire treasury).
8. The proposal passes governance vote and executes, draining the full treasury — far beyond the intended 300,000 XDR cap.

The root cause is in `fetch_icps_per_sns_token` returning an unclamped near-zero value, and `ProposalsAmountTotalUpperBound::in_tokens` having no floor on `icps_per_token`. [10](#0-9) [11](#0-10)

### Citations

**File:** rs/sns/governance/token_valuation/src/lib.rs (L314-415)
```rust
    async fn fetch_icps_per_sns_token(&self) -> Result<Decimal, ValuationError> {
        // (Concurrently) fetch the various pieces that we need to sythensize the result:
        let (get_derived_state_result, initial_supply_e8s_result, current_supply_result) = join!(
            // 1. SNS token price from swap.
            call::<_, MyRuntime>(self.swap_canister_id, GetDerivedStateRequest {}),
            // 2. Initial SNS token supply.
            initial_supply_e8s::<MyRuntime>(
                self.sns_token_ledger_canister_id,
                InitialSupplyOptions::new()
            ),
            // 3. Current SNS token supply.
            MyRuntime::call_with_cleanup::<_, (Nat,)>(
                self.sns_token_ledger_canister_id,
                "icrc1_total_supply",
                ()
            ),
        );
        // (Factors 2 and 3 tell us how much inflation there has been. For
        // example, if the amount of tokens has doubled since the beginning,
        // then the current ICPs per SNS token should be half of what it was at
        // the time of the swap.)

        // Unwrap (intermediate) results.
        let get_derived_state_response = get_derived_state_result.map_err(|err| {
            ValuationError::new_external(format!(
                "Unable to obtain SNS token price at the time of the SNS initialization swap: {err:?}",
            ))
        })?;
        let initial_supply_e8s = initial_supply_e8s_result.map_err(|err| {
            ValuationError::new_external(format!(
                "Unable to determine the initial supply of SNS tokens: {err:?}",
            ))
        })?;
        let (current_supply_e8s,) = current_supply_result.map_err(|err| {
            ValuationError::new_external(format!(
                "Unable to obtain the current supply of SNS tokens: {err:?}",
            ))
        })?;

        // Read the relevant fields.

        // Here, a floating point field is used. This is ok, because we are just
        // using this to come up with a valuation, which isn't an exact science.
        let initial_sns_tokens_per_icp: f64 = get_derived_state_response
            .sns_tokens_per_icp
            .ok_or_else(|| {
                ValuationError::new_mismatch(format!(
                    "Response from swap ({}) get_derived_state call did not \
                     contain sns_tokens_per_icp: {:#?}",
                    self.swap_canister_id, get_derived_state_response,
                ))
            })?;

        // Convert all numbers to Decimal.

        let initial_sns_tokens_per_icp = Decimal::from_f64_retain(initial_sns_tokens_per_icp)
            .ok_or_else(|| {
                ValuationError::new_arithmetic(format!(
                    "Unable to convert sns_tokens_per_icp {initial_sns_tokens_per_icp} (double precision \
                     floating point) to Decimal.",
                ))
            })?;

        let initial_supply_e8s = i2d(initial_supply_e8s);

        let current_supply_e8s =
            Decimal::from(current_supply_e8s.0.to_u128().ok_or_else(|| {
                ValuationError::new_arithmetic(format!(
                    "Unable to convert current_supply_e8s ({current_supply_e8s}) from Nat to Decimal.",
                ))
            })?);

        // Do actual (simple) math.

        // Flip the ratio from SNS tokens per ICP to ICPs per SNS token.
        let initial_icps_per_sns_token = Decimal::from(1)
            .checked_div(initial_sns_tokens_per_icp)
            .ok_or_else(|| {
            ValuationError::new_arithmetic(format!(
                "Unable to perform 1 / sns_tokens_per_icp (where sns_tokens_per_icp = {initial_sns_tokens_per_icp}).",
            ))
        })?;

        let total_inflation = current_supply_e8s
            .checked_div(initial_supply_e8s)
            .ok_or_else(|| {
                ValuationError::new_arithmetic(format!(
                    "Unable to perform current_supply / initial_supply \
                     (where current_supply_e8s = {current_supply_e8s} and initial_supply_e8s = {initial_supply_e8s})",
                ))
            })?;

        // Finally, current price = initial price scaled down by inflation (or deflation).
        initial_icps_per_sns_token
            .checked_div(total_inflation)
            .ok_or_else(|| {
                ValuationError::new_arithmetic(format!(
                    "Unable to perform initial_icps_per_sns_token / total_inflation \
                     (where initial_icps_per_sns_token = {initial_icps_per_sns_token} and total_inflation = {total_inflation})",
                ))
            })
    }
```

**File:** rs/sns/swap/src/swap.rs (L2992-2995)
```rust
        let sns_tokens_per_icp = i2d(tokens_available_for_swap)
            .checked_div(i2d(participant_total_icp_e8s))
            .and_then(|d| d.to_f32())
            .unwrap_or(0.0);
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L60-64)
```rust
    /// # Why Not Also Define MAX?
    ///
    /// Currently, we do not have/enforce a MAX_XDRS_PER_ICP, because this would tend to cause our
    /// valuations to be in the "large" regime, where actions are more limited.
    const MIN_XDRS_PER_ICP: Decimal = dec!(1);
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L66-75)
```rust
    fn in_tokens(mut valuation: Valuation) -> Result<Decimal, ProposalsAmountTotalLimitError> {
        Self::clamp_xdrs_per_icp(&mut valuation);

        let ValuationFactors {
            tokens: balance_tokens,
            icps_per_token,
            xdrs_per_icp,
        } = valuation.valuation_factors;

        let self_ = Self::from_valuation_xdr(valuation.to_xdr());
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L76-78)
```rust
        let result_tokens = match self_ {
            Self::NoLimit => balance_tokens,

```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L116-134)
```rust
    fn from_valuation_xdr(valuation_xdr: Decimal) -> Self {
        // Ideally, this would be checked at compile time. In principal, this should be possible,
        // since all the inputs are const, but I'm not sure how to do that. Therefore,
        // debug_assert_eq is used instead, and should be very nearly as good, because this will be
        // run during CI.
        debug_assert_eq!(
            Self::MAX_MEDIUM_TREASURY_SIZE_XDR.checked_mul(ONE_QUARTER),
            Some(Self::MAX_XDR),
        );

        if valuation_xdr <= Self::MAX_SMALL_TREASURY_SIZE_XDR {
            return Self::NoLimit;
        }

        if valuation_xdr <= Self::MAX_MEDIUM_TREASURY_SIZE_XDR {
            return Self::Fraction(ONE_QUARTER);
        }

        Self::Xdr(Self::MAX_XDR)
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L137-140)
```rust
    fn clamp_xdrs_per_icp(valuation: &mut Valuation) {
        let xdrs_per_icp = &mut valuation.valuation_factors.xdrs_per_icp;
        *xdrs_per_icp = (*xdrs_per_icp).max(Self::MIN_XDRS_PER_ICP);
    }
```

**File:** rs/sns/governance/src/governance.rs (L2203-2211)
```rust
            Action::TransferSnsTreasuryFunds(transfer) => {
                let valuation =
                    get_action_auxiliary(&self.proto.proposals, ProposalId { id: proposal_id })
                        .and_then(|action_auxiliary| {
                            action_auxiliary.unwrap_transfer_sns_treasury_funds_or_err()
                        });
                self.perform_transfer_sns_treasury_funds(proposal_id, valuation, &transfer)
                    .await
            }
```

**File:** rs/sns/governance/src/proposal.rs (L2600-2617)
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
```
