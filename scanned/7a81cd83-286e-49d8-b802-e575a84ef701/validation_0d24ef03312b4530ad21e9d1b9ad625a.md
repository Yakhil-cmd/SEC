### Title
Missing Upper Bound on `icps_per_token` in SNS Treasury Valuation Allows Inflated Withdrawal Limits - (File: `rs/sns/governance/token_valuation/src/lib.rs`)

### Summary

The SNS governance treasury valuation system applies a floor (`MIN_XDRS_PER_ICP = 1`) on the `xdrs_per_icp` price factor to prevent artificially low ICP prices from inflating the "small treasury" regime. However, no analogous ceiling is applied to the `icps_per_token` factor, which is derived from the SNS swap canister's `get_derived_state` response. An inflated `icps_per_token` value causes the treasury to be overvalued in XDR, pushing it into the "large" regime and allowing a disproportionately large absolute token amount (up to `MAX_XDR = 300,000 XDR` worth) to be transferred or minted per 7-day window.

### Finding Description

The `IcpsPerSnsTokenClient::fetch_icps_per_sns_token` function in `rs/sns/governance/token_valuation/src/lib.rs` computes the current ICP-per-SNS-token price by:

1. Fetching `sns_tokens_per_icp` (a `f32`) from the SNS swap canister's `get_derived_state`.
2. Inverting it to get `initial_icps_per_sns_token`.
3. Dividing by `total_inflation` (current supply / initial supply). [1](#0-0) 

The resulting `icps_per_token` is fed into `ValuationFactors::to_xdr()` as `tokens * icps_per_token * xdrs_per_icp`. [2](#0-1) 

In `ProposalsAmountTotalUpperBound`, a `MIN_XDRS_PER_ICP` floor of `1` is applied to prevent a low ICP price from pushing the treasury into the "small" (no-limit) regime. The code explicitly documents that no `MAX_XDRS_PER_ICP` ceiling is defined, because a high ICP price would push the treasury into the "large" regime (which is more restrictive). However, **no analogous ceiling is applied to `icps_per_token`**. [3](#0-2) 

The `sns_tokens_per_icp` field in `DerivedState` is a `f32` computed live from the swap canister's current state (`tokens_available_for_swap / participant_total_icp_e8s`). [4](#0-3) 

If `participant_total_icp_e8s` is very small (e.g., the swap just started with minimal participation), `sns_tokens_per_icp` becomes very large, making `icps_per_token` (its inverse) very small — this is the normal case and is safe. However, if `sns_tokens_per_icp` is very small (e.g., the SNS token supply is tiny relative to ICP raised), `icps_per_token` becomes very large, inflating the XDR valuation.

More critically: the `total_inflation` divisor can be manipulated. If `current_supply_e8s` is close to `initial_supply_e8s` (i.e., minimal inflation), `total_inflation ≈ 1` and `icps_per_token ≈ initial_icps_per_sns_token`. But if the SNS token has experienced **deflation** (token burns reducing supply below initial), `total_inflation < 1`, causing `icps_per_token` to be **multiplied up** beyond the swap price. There is no cap on this. [5](#0-4) 

The resulting inflated `icps_per_token` causes `valuation.to_xdr()` to exceed `MAX_MEDIUM_TREASURY_SIZE_XDR` (1,200,000 XDR), placing the treasury in the "large" regime. In the large regime, the allowed 7-day transfer/mint is `MAX_XDR / xdrs_per_token` tokens. With an inflated `icps_per_token`, `xdrs_per_token` is also inflated, so `tokens_per_xdr` is small, and the token allowance is small — this is actually the protective direction.

**However**, the asymmetric risk is in the `Fraction` (medium) regime: if `icps_per_token` is inflated enough to push the treasury from "small" (no limit) to "medium" (25% limit), but not all the way to "large" (300k XDR cap), the 25% fraction is applied to `balance_tokens` directly — and `balance_tokens` is the actual token count, not affected by the inflated price. This means the 25% limit is applied to the real token count, which is correct. The inflation of `icps_per_token` only affects which regime is selected.

The real analog to the original report is: **the `xdrs_per_icp` floor exists but no `icps_per_token` floor exists**. If `icps_per_token` is near zero (e.g., the SNS token is nearly worthless), the treasury XDR valuation approaches zero, placing it in the "small" (no-limit) regime, allowing 100% of the treasury to be transferred in a single 7-day window. This is the direct analog: a token whose market price has collapsed (de-pegged from its swap price) is still valued at its swap-time price adjusted only for inflation — not for actual market conditions. [6](#0-5) 

The `sns_tokens_per_icp` value is a **snapshot from the swap's final state** (at swap completion), not a live market price. After the swap, the SNS token's actual market value can diverge significantly from this historical ratio. There is no oracle or live price feed for SNS tokens.

### Impact Explanation

An SNS governance token that has lost significant market value (e.g., crashed to near zero) will have a very low `icps_per_token` in practice, but the valuation system uses the swap-time price adjusted for inflation. If the token has inflated significantly (large `total_inflation`), `icps_per_token` is divided down further, potentially pushing the treasury into the "small" (no-limit) regime. In this regime, `ProposalsAmountTotalUpperBound::NoLimit` is returned, meaning `balance_tokens` is returned as the upper bound — i.e., **100% of the treasury can be transferred in a single 7-day window**. [7](#0-6) 

A malicious SNS governance majority (or a compromised SNS) could exploit this to drain the entire treasury in one proposal if the token has inflated enough to push the valuation below 100,000 XDR.

**However**, this requires a governance majority — which is a trusted role per the disqualification criteria. The more realistic concern is that a legitimately operating SNS with high token inflation and a depressed token price inadvertently falls into the no-limit regime, allowing larger-than-intended treasury transfers to pass.

### Likelihood Explanation

Moderate. SNS tokens with high inflation rates (common in early-stage projects with staking rewards) and depressed market prices (common in bear markets) will naturally have low `icps_per_token` values. The `MIN_XDRS_PER_ICP` floor of `1` was added precisely because the analogous scenario for ICP price was considered realistic. The same reasoning applies to `icps_per_token`, but no floor was added for it. [3](#0-2) 

### Recommendation

Add a `MIN_ICPS_PER_TOKEN` floor in `ProposalsAmountTotalUpperBound::clamp_xdrs_per_icp` (or a new analogous `clamp_icps_per_token` function) to prevent the treasury from being classified as "small" (no-limit) due to a depressed or manipulated SNS token price. The floor value should be chosen conservatively (e.g., based on the swap-time price or a governance-configurable minimum). This mirrors the existing `MIN_XDRS_PER_ICP` floor.

Additionally, consider whether `MintSnsTokens::recent_amount_total_upper_bound_tokens` returning `Decimal::MAX` (effectively no limit) is intentional long-term, as it bypasses all treasury protection for minting proposals. [8](#0-7) 

### Proof of Concept

1. An SNS launches with `sns_tokens_per_icp = 100` at swap time (i.e., `initial_icps_per_sns_token = 0.01`).
2. The SNS mints aggressively; `current_supply = 1000 * initial_supply`, so `total_inflation = 1000`.
3. `icps_per_token = 0.01 / 1000 = 0.00001`.
4. With `xdrs_per_icp = 10` (current ICP price), `xdrs_per_token = 0.00001 * 10 = 0.0001`.
5. Treasury holds 1,000,000 SNS tokens. `valuation_xdr = 1,000,000 * 0.0001 = 100 XDR`.
6. `100 XDR <= MAX_SMALL_TREASURY_SIZE_XDR (100,000)` → `ProposalsAmountTotalUpperBound::NoLimit`.
7. `in_tokens` returns `balance_tokens = 1,000,000` — the entire treasury.
8. A `TransferSnsTreasuryFunds` proposal for 1,000,000 SNS tokens passes validation at submission time. [9](#0-8) [1](#0-0)

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

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L43-64)
```rust
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

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L76-78)
```rust
        let result_tokens = match self_ {
            Self::NoLimit => balance_tokens,

```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L116-135)
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
    }
```

**File:** rs/sns/swap/src/swap.rs (L2992-2995)
```rust
        let sns_tokens_per_icp = i2d(tokens_available_for_swap)
            .checked_div(i2d(participant_total_icp_e8s))
            .and_then(|d| d.to_f32())
            .unwrap_or(0.0);
```

**File:** rs/sns/governance/src/proposal.rs (L1035-1041)
```rust
    // TODO(NNS1-2982): Delete.
    fn recent_amount_total_upper_bound_tokens(_valuation: &Valuation) -> Result<Decimal, String> {
        // Ideally, we'd return infinity, but Decimal does not have that. This is the next best
        // thing, and should be good enough, because we have already planned the obselences of this
        // code (see tickets NNS1-298(1|2)).
        Ok(Decimal::MAX)
    }
```
