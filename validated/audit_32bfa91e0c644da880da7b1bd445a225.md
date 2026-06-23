### Title
Missing `icps_per_token` Zero-Price Floor Allows SNS Treasury Transfer Limit Bypass - (`File: rs/sns/governance/proposals_amount_total_limit/src/lib.rs`)

---

### Summary

The SNS governance treasury-transfer rate-limiter clamps `xdrs_per_icp` to a minimum of `1` to prevent an artificially low ICP/XDR price from collapsing the treasury valuation to zero and bypassing the limit. However, the analogous factor `icps_per_token` — the SNS-token-to-ICP price — has **no such floor**. When `icps_per_token` rounds to zero (which can happen legitimately via floating-point precision loss in the swap canister's `f32` field), the treasury XDR valuation collapses to zero, the limit logic takes the `NoLimit` branch, and an unlimited treasury transfer or mint proposal is allowed to execute.

---

### Finding Description

The SNS governance canister enforces a 7-day rolling cap on `TransferSnsTreasuryFunds` and `MintSnsTokens` proposals via `ProposalsAmountTotalUpperBound::in_tokens()` in `rs/sns/governance/proposals_amount_total_limit/src/lib.rs`.

The cap is computed from a `Valuation` containing three factors:

```
XDR value = tokens × icps_per_token × xdrs_per_icp
```

The code explicitly clamps `xdrs_per_icp` to a minimum of `1` XDR/ICP:

```rust
fn clamp_xdrs_per_icp(valuation: &mut Valuation) {
    let xdrs_per_icp = &mut valuation.valuation_factors.xdrs_per_icp;
    *xdrs_per_icp = (*xdrs_per_icp).max(Self::MIN_XDRS_PER_ICP);
}
``` [1](#0-0) 

But `icps_per_token` receives **no analogous floor**. It is sourced from `IcpsPerSnsTokenClient::fetch_icps_per_sns_token()`, which reads `sns_tokens_per_icp` from the swap canister's `get_derived_state` response:

```rust
let initial_sns_tokens_per_icp: f64 = get_derived_state_response
    .sns_tokens_per_icp
    .ok_or_else(|| { ... })?;
``` [2](#0-1) 

That field is stored as `f32` in `DerivedState`:

```rust
pub sns_tokens_per_icp: f32,
``` [3](#0-2) 

It is computed in `Swap::derived_state()` as:

```rust
let sns_tokens_per_icp = i2d(tokens_available_for_swap)
    .checked_div(i2d(participant_total_icp_e8s))
    .and_then(|d| d.to_f32())
    .unwrap_or(0.0);
``` [4](#0-3) 

The comment in the code even acknowledges this: *"`sns_tokens_per_icp` will be 0 if `participant_total_icp_e8s` is 0."* [5](#0-4) 

When `sns_tokens_per_icp` is `0.0` (or subnormal), `Decimal::from_f64_retain(0.0)` succeeds and returns `Decimal::ZERO`. Then `1 / initial_sns_tokens_per_icp` via `checked_div` returns `None`, which propagates as a `ValuationError::Arithmetic`. However, if `sns_tokens_per_icp` is a very large number (e.g., an SNS token is nearly worthless — many tokens per ICP), the inversion `1 / sns_tokens_per_icp` produces a very small but non-zero `Decimal`. This small `icps_per_token` multiplied by `xdrs_per_icp` (clamped to 1) and `tokens` can still yield a total XDR valuation below `MAX_SMALL_TREASURY_SIZE_XDR` (100,000 XDR), causing `from_valuation_xdr` to return `NoLimit`:

```rust
if valuation_xdr <= Self::MAX_SMALL_TREASURY_SIZE_XDR {
    return Self::NoLimit;
}
``` [6](#0-5) 

When `NoLimit` is returned, the full treasury balance is returned as the allowance:

```rust
Self::NoLimit => balance_tokens,
``` [7](#0-6) 

The asymmetry is explicit in the code comments: a `MIN_XDRS_PER_ICP` floor exists to prevent the "small treasury" regime from being triggered by a wacky price quote, but **no `MIN_ICPS_PER_TOKEN` floor exists**:

```rust
/// # Why Not Also Define MAX?
/// Currently, we do not have/enforce a MAX_XDRS_PER_ICP, because this would tend to cause our
/// valuations to be in the "large" regime, where actions are more limited.
const MIN_XDRS_PER_ICP: Decimal = dec!(1);
``` [8](#0-7) 

The same asymmetry applies in the `Xdr` branch: `xdrs_per_token = xdrs_per_icp × icps_per_token`. If `icps_per_token` is extremely small (but non-zero), `xdrs_per_token` can be so small that `tokens_per_xdr = xdrs_per_token.inv()` becomes astronomically large, causing `max_xdr.checked_mul(tokens_per_xdr)` to overflow and return `None` — which is treated as an error, not as a limit bypass. However, the `NoLimit` path via a collapsed XDR valuation is the more direct concern. [9](#0-8) 

---

### Impact Explanation

An SNS whose token is nearly worthless (very high `sns_tokens_per_icp`, e.g., a meme token with 10^9 tokens per ICP) will have `icps_per_token` ≈ 10^-9. With `xdrs_per_icp` clamped to 1 and even a large token balance, the XDR valuation may fall below 100,000 XDR, triggering `NoLimit`. This allows SNS governance proposals to transfer or mint **the entire treasury** within a 7-day window, bypassing the intended rate limit. For a large-treasury SNS with a low-value token, this means an attacker who controls enough voting power to pass a proposal (which is a separate governance threshold, not a security boundary here) can drain the entire treasury in a single proposal execution, rather than being limited to 300,000 XDR worth per 7 days.

---

### Likelihood Explanation

This is reachable by any SNS governance participant who can pass a `TransferSnsTreasuryFunds` or `MintSnsTokens` proposal. The condition (very low `icps_per_token`) is realistic for SNS tokens with high supply or low market value. The `sns_tokens_per_icp` value is a live `f32` read from the swap canister's derived state, which is computed from actual participation data and can legitimately be very large. No privileged access is required beyond normal SNS governance participation.

---

### Recommendation

Add a `MIN_ICPS_PER_TOKEN` floor analogous to `MIN_XDRS_PER_ICP`, clamping `icps_per_token` before it is used in the valuation computation. The motivation mirrors the existing comment: an unrealistically low `icps_per_token` would cause the treasury to appear in the "small" regime, allowing unlimited transfers.

```rust
const MIN_ICPS_PER_TOKEN: Decimal = dec!(0.000_000_01); // e.g., 1 ICP per 100M tokens

fn clamp_icps_per_token(valuation: &mut Valuation) {
    let icps_per_token = &mut valuation.valuation_factors.icps_per_token;
    *icps_per_token = (*icps_per_token).max(Self::MIN_ICPS_PER_TOKEN);
}
```

And call it alongside `clamp_xdrs_per_icp` at the top of `in_tokens`.

---

### Proof of Concept

**Setup:** An SNS with:
- Treasury balance: 10,000,000 SNS tokens
- `sns_tokens_per_icp` = 10,000,000 (token is nearly worthless)
- `icps_per_token` = 1 / 10,000,000 = 0.0000001
- `xdrs_per_icp` = 0.5 (clamped to 1 by `MIN_XDRS_PER_ICP`)

**Valuation:** `10,000,000 × 0.0000001 × 1 = 1 XDR`

**Result:** `1 XDR ≤ MAX_SMALL_TREASURY_SIZE_XDR (100,000 XDR)` → `NoLimit` → allowance = 10,000,000 tokens.

**Attack:** Submit a `TransferSnsTreasuryFunds` proposal for the full 10,000,000 tokens. The limit check at proposal submission time calls `transfer_sns_treasury_funds_7_day_total_upper_bound_tokens(valuation)`, which returns `NoLimit` = full balance. The proposal passes the amount check and, if adopted by governance, executes the full treasury drain. [10](#0-9) [11](#0-10) [4](#0-3)

### Citations

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

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L66-114)
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
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L126-128)
```rust
        if valuation_xdr <= Self::MAX_SMALL_TREASURY_SIZE_XDR {
            return Self::NoLimit;
        }
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L137-140)
```rust
    fn clamp_xdrs_per_icp(valuation: &mut Valuation) {
        let xdrs_per_icp = &mut valuation.valuation_factors.xdrs_per_icp;
        *xdrs_per_icp = (*xdrs_per_icp).max(Self::MIN_XDRS_PER_ICP);
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

**File:** rs/sns/swap/src/gen/ic_sns_swap.pb.v1.rs (L1064-1065)
```rust
    #[prost(float, tag = "2")]
    pub sns_tokens_per_icp: f32,
```

**File:** rs/sns/swap/src/swap.rs (L2978-2979)
```rust
    /// Computes the DerivedState.
    /// `sns_tokens_per_icp` will be 0 if `participant_total_icp_e8s` is 0.
```

**File:** rs/sns/swap/src/swap.rs (L2992-2995)
```rust
        let sns_tokens_per_icp = i2d(tokens_available_for_swap)
            .checked_div(i2d(participant_total_icp_e8s))
            .and_then(|d| d.to_f32())
            .unwrap_or(0.0);
```
