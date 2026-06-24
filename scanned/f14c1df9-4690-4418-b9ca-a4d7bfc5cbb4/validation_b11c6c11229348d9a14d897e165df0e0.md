### Title
Hardcoded 8-Decimal Assumption in SNS Treasury Valuation Bypasses 7-Day Transfer Limits - (File: rs/sns/governance/token_valuation/src/lib.rs)

---

### Summary

The SNS governance treasury protection system unconditionally divides raw `icrc1_balance_of` responses by the hardcoded constant `E8` (10^8) when computing token valuations used to enforce 7-day transfer limits on `TransferSnsTreasuryFunds` and `MintSnsTokens` proposals. Because the ICRC-1 standard permits any number of decimals, SNS tokens with fewer than 8 decimals (e.g., 6) cause the treasury to appear proportionally smaller than it is, making the limit proportionally more permissive and allowing governance proposals to drain the treasury faster than the protocol intends.

---

### Finding Description

In `rs/sns/governance/token_valuation/src/lib.rs`, the function `try_get_balance_valuation_factors` fetches the raw atomic-unit balance from `icrc1_balance_of` and converts it to a human-readable token count by dividing by the hardcoded constant `E8 = 100_000_000`:

```rust
let tokens = Decimal::from(u128::try_from(balance_of_response.0)...) / Decimal::from(E8);
``` [1](#0-0) 

This `tokens` value feeds directly into `ValuationFactors::to_xdr()`:

```rust
tokens * icps_per_token * xdrs_per_icp
``` [2](#0-1) 

The resulting XDR valuation is used by `ProposalsAmountTotalUpperBound::in_tokens` to determine the 7-day treasury transfer ceiling: [3](#0-2) 

Simultaneously, `proposal_amount_tokens()` for both `TransferSnsTreasuryFunds` and `MintSnsTokens` also hardcodes `E8` when converting the user-supplied `amount_e8s` field to tokens:

```rust
denominations_to_tokens(amount_e8s, E8)
``` [4](#0-3) [5](#0-4) 

The ICRC-1 ledger used for SNS tokens supports a configurable `decimals` field (e.g., 6 for USDC-style tokens): [6](#0-5) 

No code in the SNS governance or token valuation path queries `icrc1_decimals` or adjusts the divisor accordingly. The assumption that all SNS tokens use 8 decimals is implicit and undocumented.

---

### Impact Explanation

**For a 6-decimal SNS token:**

- A treasury holding 1,000,000 tokens has a raw balance of `1,000,000 × 10^6 = 10^12` atomic units.
- Divided by `E8 = 10^8`, the code computes `tokens = 10,000` — **100× smaller** than the actual 1,000,000.
- The XDR valuation is 100× understated, placing the treasury in a smaller regime (e.g., "small" instead of "large").
- The 7-day transfer ceiling is 100× more permissive than intended.
- A governance proposal can transfer 100× more tokens per 7-day window than the protocol's treasury protection intends.
- Simultaneously, `proposal_amount_tokens` = `amount_e8s / E8` = `10^6 / 10^8 = 0.01` for 1 token, so the proposal amount appears 100× smaller, further inflating the effective allowance.
- Net effect: the 7-day treasury drain protection is **effectively nullified** for 6-decimal SNS tokens.

**For an 18-decimal SNS token:** the valuation is inflated 10^10×, making the limit overly restrictive (a denial-of-service on treasury operations, not a drain risk).

---

### Likelihood Explanation

The ICRC-1 standard explicitly allows any `decimals` value. The SNS framework's `InitArgs` accepts `decimals: Option<u8>` with no enforcement of 8. Projects deploying SNS DAOs with ERC-20-style tokens (6 decimals) or custom tokens are directly affected. The entry path requires only a standard SNS governance proposal submission by any neuron holder — no privileged access is needed. The `treasury_valuation_if_proposal_amount_is_small_enough_or_err` function is called on every `TransferSnsTreasuryFunds` and `MintSnsTokens` proposal submission. [7](#0-6) 

---

### Recommendation

1. In `try_get_balance_valuation_factors`, query `icrc1_decimals` from the ledger canister alongside the balance, and use `10^decimals` as the divisor instead of the hardcoded `E8`.
2. In `proposal_amount_tokens()` for both `TransferSnsTreasuryFunds` and `MintSnsTokens`, use the actual token decimals (stored in governance state or fetched at proposal time) rather than the hardcoded `E8`.
3. Document the decimal assumption explicitly, or enforce at SNS initialization that the SNS token ledger must use exactly 8 decimals.

---

### Proof of Concept

1. Deploy an SNS with a 6-decimal ICRC-1 token ledger (set `decimals = 6` in `InitArgs`).
2. Fund the SNS treasury with 1,000,000 tokens (raw balance = `10^12` atomic units).
3. Submit a `TransferSnsTreasuryFunds` proposal for 10,000 tokens (raw `amount_e8s = 10^10`).
4. The governance canister calls `try_get_sns_token_balance_valuation`, which computes `tokens = 10^12 / 10^8 = 10,000` (actual: 1,000,000).
5. The XDR valuation is 100× understated → treasury classified as "small" → `NoLimit` regime → no 7-day cap enforced.
6. `proposal_amount_tokens = 10^10 / 10^8 = 100` (actual: 10,000 tokens).
7. The proposal passes the limit check and executes, transferring 10,000 tokens — far exceeding what the "large treasury" regime would have permitted (300,000 XDR worth).
8. Repeat every 7 days to drain the treasury at 100× the intended rate. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/sns/governance/token_valuation/src/lib.rs (L118-126)
```rust
    pub fn to_xdr(&self) -> Decimal {
        let Self {
            tokens,
            icps_per_token,
            xdrs_per_icp,
        } = self;

        tokens * icps_per_token * xdrs_per_icp
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

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L116-140)
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

    fn clamp_xdrs_per_icp(valuation: &mut Valuation) {
        let xdrs_per_icp = &mut valuation.valuation_factors.xdrs_per_icp;
        *xdrs_per_icp = (*xdrs_per_icp).max(Self::MIN_XDRS_PER_ICP);
    }
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

**File:** rs/sns/governance/src/proposal.rs (L1003-1015)
```rust
    fn proposal_amount_tokens(&self) -> Result<Decimal, String> {
        let amount_e8s = self
            .amount_e8s
            // This Err only occurs when self is invalid.
            .ok_or_else(|| "The `amount_e8s` field is not populated.".to_string())?;

        denominations_to_tokens(amount_e8s, E8)
            // This Err will not be generated, because we are dividing a u64 (amount_e8s) by a
            // positive number (E8).
            .ok_or_else(
                || format!("Unable to convert proposal amount {amount_e8s} e8s to tokens.",),
            )
    }
```

**File:** rs/sns/governance/src/proposal.rs (L2632-2643)
```rust
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
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L855-857)
```rust
    pub fn decimals(&self) -> u8 {
        self.decimals
    }
```
