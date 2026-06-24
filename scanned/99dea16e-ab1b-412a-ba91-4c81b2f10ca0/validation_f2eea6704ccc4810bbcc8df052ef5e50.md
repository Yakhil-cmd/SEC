### Title
Hardcoded `E8` Divisor in SNS Treasury Valuation Produces Wrong Tier Classification for Non-8-Decimal SNS Tokens - (File: `rs/sns/governance/token_valuation/src/lib.rs`)

### Summary
`try_get_balance_valuation_factors` unconditionally divides the raw ICRC-1 balance by the hardcoded constant `E8` (10^8) to convert atomic units to whole tokens. Because ICRC-1 tokens can be deployed with any number of decimals, this assumption is incorrect for SNS tokens with fewer than 8 decimals (e.g., 6-decimal tokens). The resulting `tokens` value is used to compute the XDR valuation of the treasury, which determines which rate-limit tier applies to `TransferSnsTreasuryFunds` and `MintSnsTokens` proposals. A wrong tier classification can allow the entire treasury to be drained in a single proposal window, bypassing the intended 7-day rate limit.

### Finding Description
In `try_get_balance_valuation_factors`, the raw balance returned by `icrc1_balance_of` is divided by the hardcoded `E8 = 100_000_000`:

```rust
// rs/sns/governance/token_valuation/src/lib.rs, line 177-181
let tokens = Decimal::from(u128::try_from(balance_of_response.0).map_err(|err| {
    ValuationError::new_arithmetic(format!(
        "Balance of {account:?} does not fit in u128: {err:?}"
    ))
})?) / Decimal::from(E8);
``` [1](#0-0) 

`E8` is defined as `100_000_000` (10^8): [2](#0-1) 

The resulting `tokens` field in `ValuationFactors` is then used in `to_xdr()` to compute the treasury's XDR value:

```rust
// rs/sns/governance/token_valuation/src/lib.rs, line 118-126
impl ValuationFactors {
    pub fn to_xdr(&self) -> Decimal {
        tokens * icps_per_token * xdrs_per_icp
    }
}
``` [3](#0-2) 

This XDR value is fed into `ProposalsAmountTotalUpperBound::from_valuation_xdr`, which classifies the treasury as small (<100K XDR), medium (100K–1.2M XDR), or large (>1.2M XDR): [4](#0-3) 

For the **small** tier, the limit is `NoLimit` — the entire `balance_tokens` can be transferred. For the **large** tier, the limit is capped at 300,000 XDR worth of tokens.

The proposal amount is also converted using the same hardcoded `E8`:

```rust
// rs/sns/governance/src/proposal.rs, line 839-848
fn proposal_amount_tokens(&self) -> Result<Decimal, String> {
    denominations_to_tokens(self.amount_e8s, E8)
``` [5](#0-4) 

And at execution time: [6](#0-5) 

Because both the balance and the proposal amount are divided by the same wrong constant, the direct comparison `proposal_amount_tokens <= max_tokens` still holds numerically. However, the **tier classification** is based solely on the XDR valuation, which is computed from the incorrectly scaled `balance_tokens`. This is where the error has real impact.

### Impact Explanation
Consider an SNS token with **6 decimals** (1 token = 10^6 atomic units):

| | Correct (6 decimals) | Computed (hardcoded E8) |
|---|---|---|
| Treasury: 10M tokens = 10^13 atomic units | 10,000,000 tokens | 100,000 tokens |
| XDR value (at 1 ICP/token, 1 XDR/ICP) | 10,000,000 XDR → **large** tier | 100,000 XDR → **small** tier |
| 7-day transfer limit | 300,000 tokens | **100% of treasury = 10,000,000 tokens** |

A governance proposal to transfer the entire 10,000,000-token treasury passes the limit check because the treasury is misclassified as "small." The intended 300,000-token cap (300K XDR) is completely bypassed — a **33× overallowance**.

The same logic applies to `MintSnsTokens` proposals, which use the same valuation path.

### Likelihood Explanation
- ICRC-1 tokens support arbitrary decimals; there is no protocol-level restriction preventing an SNS from using 6-decimal tokens.
- An SNS with a 6-decimal token is a valid, deployable configuration.
- Any SNS token holder with sufficient voting power to pass a `TransferSnsTreasuryFunds` proposal can exploit this. The treasury rate limit is specifically designed to protect against a compromised or colluding majority — this bug nullifies that protection for affected SNS instances.
- Likelihood is **medium**: requires an SNS deployed with non-8-decimal tokens, but no privileged access beyond normal SNS governance participation.

### Recommendation
Replace the hardcoded `E8` divisor in `try_get_balance_valuation_factors` with a dynamically fetched `icrc1_decimals` value from the ledger canister. The balance should be divided by `10^decimals` rather than the fixed `10^8`. Similarly, `TransferSnsTreasuryFunds.amount_e8s` (and the corresponding `MintSnsTokens` field) should be interpreted using the actual token decimals, not a hardcoded 8.

### Proof of Concept
1. Deploy an SNS with a token configured to use **6 decimals**.
2. Fund the SNS treasury with 10,000,000 tokens (10^13 atomic units).
3. Submit a `TransferSnsTreasuryFunds` proposal with `amount_e8s = 10^13` (the entire treasury).
4. Observe that `try_get_balance_valuation_factors` computes `tokens = 10^13 / 10^8 = 100,000`, yielding an XDR valuation of 100,000 XDR — placing the treasury in the "small" tier with `NoLimit`.
5. The proposal passes the limit check (`10^13 / 10^8 <= 10^13 / 10^8`) and, if voted through, drains the entire treasury — far exceeding the intended 300,000-token cap. [7](#0-6) [8](#0-7) [9](#0-8)

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

**File:** rs/sns/governance/token_valuation/src/lib.rs (L176-191)
```rust
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

**File:** rs/nervous_system/common/src/lib.rs (L60-61)
```rust
// 10^8
pub const E8: u64 = 100_000_000;
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L34-41)
```rust
impl ProposalsAmountTotalUpperBound {
    // A treasury can be small, medium, or large. These are the boundaries between those regimes.
    const MAX_SMALL_TREASURY_SIZE_XDR: Decimal = dec!(100_000);
    const MAX_MEDIUM_TREASURY_SIZE_XDR: Decimal = dec!(1_200_000);

    // No matter how large the treasury is, not more than this amount can be removed (within a 7 day
    // window).
    const MAX_XDR: Decimal = dec!(300_000);
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

**File:** rs/sns/governance/src/proposal.rs (L839-848)
```rust
    fn proposal_amount_tokens(&self) -> Result<Decimal, String> {
        denominations_to_tokens(self.amount_e8s, E8)
            // This Err will not be generated, because we are dividing a u64 (amount_e8s) by a
            // positive number (E8).
            .ok_or_else(|| {
                format!(
                    "Unable to convert proposal amount {} e8s to tokens.",
                    self.amount_e8s,
                )
            })
```

**File:** rs/sns/governance/src/proposal.rs (L2600-2658)
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

    Ok(())
```
