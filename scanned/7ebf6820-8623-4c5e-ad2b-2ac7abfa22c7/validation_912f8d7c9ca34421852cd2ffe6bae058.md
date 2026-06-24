### Title
Hardcoded `E8` Divisor in `try_get_balance_valuation_factors` Assumes 8-Decimal SNS Token, Causing Incorrect Treasury Valuation and Wrong Transfer Limit Enforcement - (File: `rs/sns/governance/token_valuation/src/lib.rs`)

---

### Summary

In `try_get_balance_valuation_factors`, the raw ICRC-1 balance (returned in the token's native smallest unit) is always divided by the hardcoded constant `E8` (= 100_000_000, i.e. 10^8) to convert it to a "whole token" count. This is correct only when the SNS token has exactly 8 decimal places. ICRC-1 tokens can be configured with any number of decimals (0–255). When an SNS token has a different decimal count, the computed `tokens` field in `ValuationFactors` is wrong by a factor of `10^(actual_decimals - 8)`, which directly corrupts the treasury valuation used to enforce the 7-day `TransferSnsTreasuryFunds` and `MintSnsTokens` proposal spending limits.

---

### Finding Description

In `rs/sns/governance/token_valuation/src/lib.rs`, the function `try_get_balance_valuation_factors` fetches the raw ICRC-1 balance and converts it to a human-readable token count by dividing by the hardcoded constant `E8`:

```rust
let tokens = Decimal::from(u128::try_from(balance_of_response.0)...) / Decimal::from(E8);
``` [1](#0-0) 

`E8` is defined as `100_000_000` (10^8), which is the correct divisor only for tokens with exactly 8 decimal places. [2](#0-1) 

However, ICRC-1 tokens are not required to have 8 decimals. The `decimals` field in the ICRC-1 standard is a `u8`, allowing any value from 0 to 255. The SNS ledger itself exposes `icrc1_decimals` as a configurable `u8`: [3](#0-2) 

The function never queries `icrc1_decimals` from the ledger canister. It unconditionally uses `E8` regardless of the actual token precision. This is the same class of bug as the reported `pricePerShare` issue: a hardcoded decimal assumption applied to a value whose actual decimal count may differ.

The resulting `tokens` value flows directly into `ValuationFactors`, which is used by `transfer_sns_treasury_funds_7_day_total_upper_bound_tokens` and `mint_sns_tokens_7_day_total_upper_bound_tokens` to compute the 7-day spending cap: [4](#0-3) 

These limits gate execution of `TransferSnsTreasuryFunds` and `MintSnsTokens` proposals in SNS governance: [5](#0-4) [6](#0-5) 

---

### Impact Explanation

**Scenario A — SNS token with fewer than 8 decimals (e.g., 6 decimals):**
The raw balance is divided by 10^8 instead of 10^6, making the computed token count 100× smaller than reality. The treasury valuation is 100× underestimated. This causes the spending limit to be 100× too small, blocking legitimate `TransferSnsTreasuryFunds` and `MintSnsTokens` proposals that should be allowed. Governance is effectively frozen for large-but-legitimate transfers.

**Scenario B — SNS token with more than 8 decimals (e.g., 18 decimals):**
The raw balance is divided by 10^8 instead of 10^18, making the computed token count 10^10× larger than reality. The treasury valuation is 10^10× overestimated. This causes the spending limit to be 10^10× too large, allowing proposals to drain the entire treasury in a single 7-day window, bypassing the intended rate-limiting protection entirely.

In both cases, the `valuation_factors.tokens` stored in the proposal's `ActionAuxiliary` is also wrong, corrupting the on-chain audit trail. [7](#0-6) 

---

### Likelihood Explanation

The current SNS framework defaults to 8-decimal tokens, so the bug is latent for all existing SNS deployments. However, the ICRC-1 standard explicitly supports arbitrary decimals, and the SNS ledger's `InitArgs` accepts a `decimals: Option<u8>` field: [8](#0-7) 

Any SNS that initializes with a non-8-decimal token (a valid and permissible configuration) will be affected. An attacker who controls an SNS with a high-decimal token (e.g., 18 decimals) can exploit the overestimated limit to pass `MintSnsTokens` proposals that mint far beyond the intended 7-day cap, effectively minting unbounded tokens in a single window. This is reachable by any SNS token holder with sufficient voting power — no privileged access is required beyond normal SNS governance participation.

---

### Recommendation

Replace the hardcoded `E8` divisor with a dynamic query to `icrc1_decimals` on the relevant ledger canister. The `try_get_balance_valuation_factors` function (or its callers `try_get_icp_balance_valuation` and `try_get_sns_token_balance_valuation`) should fetch the token's actual decimal count and compute the divisor as `10^decimals`. For ICP specifically, the decimal count is always 8 and can remain hardcoded. For SNS tokens, the divisor must be fetched from the SNS ledger's `icrc1_decimals` endpoint. [9](#0-8) 

---

### Proof of Concept

1. Deploy an SNS with `decimals = 18` in the SNS ledger `InitArgs`.
2. Fund the SNS treasury with 1,000 SNS tokens (= 1,000 × 10^18 raw units).
3. Submit a `TransferSnsTreasuryFunds` proposal for 1,000 tokens.
4. During validation, `try_get_balance_valuation_factors` fetches the raw balance `1000 × 10^18` and divides by `E8 = 10^8`, yielding `tokens = 1000 × 10^10 = 10^13` (ten trillion tokens).
5. `transfer_sns_treasury_funds_7_day_total_upper_bound_tokens` computes the limit against this inflated valuation, producing a cap orders of magnitude above the actual treasury size.
6. The proposal passes the amount check and executes, draining the treasury — even though the intended limit should have blocked it.

The root cause is at: [1](#0-0)

### Citations

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

**File:** rs/ledger_suite/common/ledger_core/src/tokens.rs (L135-137)
```rust
pub const DECIMAL_PLACES: u32 = 8;
/// How many times can Tokens be divided
pub const TOKEN_SUBDIVIDABLE_BY: u64 = 100_000_000;
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L513-516)
```rust
#[query]
fn icrc1_decimals() -> u8 {
    Access::with_ledger(|ledger| ledger.decimals())
}
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L66-113)
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
```

**File:** rs/sns/governance/src/proposal.rs (L863-869)
```rust
    fn recent_amount_total_upper_bound_tokens(valuation: &Valuation) -> Result<Decimal, String> {
        transfer_sns_treasury_funds_7_day_total_upper_bound_tokens(*valuation)
            // Err is most likely a bug.
            .map_err(|treasury_limit_error| {
                format!("Unable to validate amount: {treasury_limit_error:?}",)
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

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L206-209)
```rust
    pub fn with_decimals(mut self, decimals: u8) -> Self {
        self.0.decimals = Some(decimals);
        self
    }
```
