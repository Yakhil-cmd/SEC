### Title
SNS Treasury Transfer Limit Bypass via Frozen Swap-Time Token Price in `IcpsPerSnsTokenClient` - (File: `rs/sns/governance/token_valuation/src/lib.rs`)

---

### Summary

The SNS governance treasury transfer limit system computes the SNS token's value using a price permanently frozen at the SNS initialization swap's exchange rate, adjusted only for token supply inflation. It does not use any current market price. Because the `ProposalsAmountTotalUpperBound` logic classifies a treasury as "small" (≤ 100,000 XDR → `NoLimit`) based on this stale valuation, a treasury whose tokens have significantly appreciated in market value since the swap can be fully drained in a single 7-day window — bypassing the rate-limiting protection that is the only on-chain safeguard against a governance-majority treasury drain.

---

### Finding Description

`IcpsPerSnsTokenClient::fetch_icps_per_sns_token()` in `rs/sns/governance/token_valuation/src/lib.rs` derives the SNS token price in three steps:

1. Calls `get_derived_state` on the swap canister to obtain `sns_tokens_per_icp`.
2. Fetches the initial and current token supply from the SNS ledger.
3. Computes: `current_icps_per_sns_token = (1 / sns_tokens_per_icp) / (current_supply / initial_supply)`. [1](#0-0) 

The `sns_tokens_per_icp` field returned by `get_derived_state` is computed inside `Swap::derived_state()` as:

```
sns_tokens_per_icp = tokens_available_for_swap / participant_total_icp_e8s
``` [2](#0-1) 

After the swap finalizes, both `tokens_available_for_swap` (the SNS tokens allocated to the swap) and `participant_total_icp_e8s` (the total ICP raised) are permanently frozen. `sns_tokens_per_icp` therefore reflects the swap-time exchange rate and never changes regardless of subsequent market price movements.

The only post-swap adjustment applied is for token supply inflation (`current_supply / initial_supply`). Pure market price appreciation — the token trading at a higher ICP price on secondary markets — is entirely invisible to this calculation.

This stale `icps_per_token` feeds directly into `ProposalsAmountTotalUpperBound::in_tokens()`: [3](#0-2) 

The regime classification is:
- Treasury XDR value ≤ 100,000 XDR → `NoLimit` (entire treasury transferable)
- 100,000–1,200,000 XDR → `Fraction(0.25)` (25% per 7 days)
- > 1,200,000 XDR → `Xdr(300_000)` (300,000 XDR per 7 days)

Critically, `clamp_xdrs_per_icp` enforces a floor on `xdrs_per_icp` to prevent artificially low ICP/XDR rates from pushing the treasury into the `NoLimit` regime: [4](#0-3) 

However, **no analogous floor or ceiling exists for `icps_per_token`**. The code comment explicitly acknowledges the asymmetry for `xdrs_per_icp` but does not address `icps_per_token`: [5](#0-4) 

The valuation is computed once at proposal submission time and stored in `ActionAuxiliary::TransferSnsTreasuryFunds`. At execution time, `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` reuses this stored valuation — the stale price is locked in permanently: [6](#0-5) 

---

### Impact Explanation

**Governance authorization bug / ledger conservation bug.**

If an SNS token's market price appreciates significantly after the swap (without proportional supply inflation), the computed `icps_per_token` is far below the true market rate. This understates the treasury's XDR value, potentially classifying a treasury worth millions of XDR as "small" (< 100,000 XDR), triggering the `NoLimit` branch.

Concrete example:
- Swap price: 10 SNS tokens per ICP → `initial_icps_per_sns_token = 0.1`
- No supply inflation since swap → `total_inflation = 1.0`
- Computed `icps_per_token = 0.1` (frozen)
- ICP/XDR rate: 10 XDR/ICP (clamped to ≥ 1, so this is fine)
- Treasury holds 100,000 SNS tokens
- **Computed treasury value**: 100,000 × 0.1 × 10 = 100,000 XDR → borderline small/medium
- **Actual market price**: 1 ICP per SNS token (10× appreciation)
- **Actual treasury value**: 100,000 × 1 × 10 = 1,000,000 XDR → large regime (300,000 XDR/7-day cap)

With a 50× appreciation and the same treasury, the computed value falls to 20,000 XDR (`NoLimit`), while the actual value is 5,000,000 XDR. A single `TransferSnsTreasuryFunds` proposal can drain the entire treasury in one 7-day window.

The same logic applies to `MintSnsTokens` proposals, which use the identical `mint_sns_tokens_7_day_total_upper_bound_tokens` path: [7](#0-6) 

---

### Likelihood Explanation

**Medium.** The preconditions are:

1. **SNS token appreciates significantly in market value** — common for successful SNS projects; a 10–100× appreciation over months/years is realistic.
2. **A governance majority submits a draining proposal** — the treasury limits exist precisely because a governance majority is not unconditionally trusted. A whale neuron holder, a coordinated group, or a compromised key can constitute a majority in many SNS deployments.

The treasury transfer rate limits are the *only* on-chain mechanism protecting against a governance-majority treasury drain. When this mechanism is bypassed due to stale pricing, the entire protection collapses. The scenario is not theoretical: SNS tokens with significant post-swap appreciation already exist on mainnet.

---

### Recommendation

**Short term:** Add a `MIN_ICPS_PER_TOKEN` floor analogous to `MIN_XDRS_PER_ICP`. Since the intent of the floor is to prevent the treasury from appearing artificially small, the floor should be set to the swap-time `icps_per_token` (i.e., never allow the computed price to fall *below* the swap price, but also never allow it to be *above* the swap price — use `max(computed, swap_time_price)`). This prevents appreciation from making the treasury appear smaller than it was at genesis.

**Long term:** Replace the swap-time frozen price with a live market price oracle (e.g., an on-chain DEX TWAP or the XRC canister if SNS/ICP pairs are supported). Until a reliable oracle exists, consider using `max(swap_time_price, inflation_adjusted_price)` as a conservative lower bound on `icps_per_token`, ensuring the treasury is never valued below its genesis-equivalent worth.

---

### Proof of Concept

**Entry path**: Any SNS neuron holder with sufficient voting power submits a `TransferSnsTreasuryFunds` proposal via the SNS governance canister's `manage_neuron` update call. No privileged access is required beyond a governance majority.

**Step-by-step**:

1. SNS launches. Swap finalizes at 10 SNS tokens per ICP. Treasury receives 200,000 SNS tokens. `sns_tokens_per_icp` in the swap canister is frozen at 10.0.

2. Over 12 months, the SNS token appreciates to 2 ICP per token on secondary markets. No new tokens are minted (supply unchanged). Actual treasury value: 200,000 × 2 × 10 XDR = 4,000,000 XDR.

3. `IcpsPerSnsTokenClient::fetch_icps_per_sns_token()` calls `get_derived_state` on the swap canister. It receives `sns_tokens_per_icp = 10.0` (frozen). With no supply inflation, it computes `icps_per_token = 1/10 = 0.1`. [8](#0-7) 

4. Treasury valuation: 200,000 × 0.1 × 10 XDR = 200,000 XDR → `Fraction(0.25)` regime → 25% cap = 50,000 SNS tokens per 7 days. (Actual regime should be `Xdr(300,000)` → ~15,000 SNS tokens per 7 days.)

5. For a more extreme case (50× appreciation, 100,000 SNS tokens): computed value = 100,000 × 0.1 × 10 = 100,000 XDR → `NoLimit`. Attacker submits a proposal to transfer all 100,000 SNS tokens (actual value: 5,000,000 XDR). Validation passes because `NoLimit` is returned. [9](#0-8) 

6. Proposal is adopted and executed. `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` reuses the stored stale valuation and also returns `NoLimit`. The full treasury is transferred. [10](#0-9)

### Citations

**File:** rs/sns/governance/token_valuation/src/lib.rs (L314-334)
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
```

**File:** rs/sns/governance/token_valuation/src/lib.rs (L357-415)
```rust
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

**File:** rs/sns/swap/src/swap.rs (L2980-2995)
```rust
    pub fn derived_state(&self) -> DerivedState {
        let participant_total_icp_e8s = self.current_total_participation_e8s();
        let direct_participant_count = Some(self.buyers.len() as u64);
        let cf_participant_count = Some(self.cf_participants.len() as u64);
        let cf_neuron_count = Some(self.cf_neuron_count());
        let tokens_available_for_swap = match self.sns_token_e8s() {
            Ok(tokens) => tokens,
            Err(err) => {
                log!(ERROR, "{}", err);
                0
            }
        };
        let sns_tokens_per_icp = i2d(tokens_available_for_swap)
            .checked_div(i2d(participant_total_icp_e8s))
            .and_then(|d| d.to_f32())
            .unwrap_or(0.0);
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L14-18)
```rust
pub fn mint_sns_tokens_7_day_total_upper_bound_tokens(
    valuation: Valuation,
) -> Result<Decimal, ProposalsAmountTotalLimitError> {
    ProposalsAmountTotalUpperBound::in_tokens(valuation)
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

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L66-134)
```rust
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

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L137-141)
```rust
    fn clamp_xdrs_per_icp(valuation: &mut Valuation) {
        let xdrs_per_icp = &mut valuation.valuation_factors.xdrs_per_icp;
        *xdrs_per_icp = (*xdrs_per_icp).max(Self::MIN_XDRS_PER_ICP);
    }
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

**File:** rs/sns/governance/src/proposal.rs (L2644-2656)
```rust
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
