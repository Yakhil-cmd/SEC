### Title
Hardcoded `E8` Divisor in SNS Treasury Valuation Ignores Actual Token Decimals - (File: rs/sns/governance/token_valuation/src/lib.rs)

### Summary
The function `try_get_balance_valuation_factors` in the SNS governance token valuation library unconditionally divides the raw ICRC-1 balance by the hardcoded constant `E8` (10^8) to convert it to a token amount. Because ICRC-1 tokens support configurable decimals, an SNS whose native token has decimals ≠ 8 will receive a systematically wrong treasury valuation, directly corrupting the 7-day spending-limit guard on `TransferSnsTreasuryFunds` proposals.

### Finding Description

In `rs/sns/governance/token_valuation/src/lib.rs`, the private function `try_get_balance_valuation_factors` is the single path used to value both ICP and SNS-token treasury balances:

```rust
// line 177-181
let tokens = Decimal::from(u128::try_from(balance_of_response.0).map_err(|err| {
    ValuationError::new_arithmetic(format!(
        "Balance of {account:?} does not fit in u128: {err:?}"
    ))
})?) / Decimal::from(E8);   // ← always 10^8, never queries icrc1_decimals
``` [1](#0-0) 

`E8` is the constant `100_000_000` (10^8), defined in `ic_nervous_system_common`: [2](#0-1) 

The ICRC-1 ledger exposes `icrc1_decimals() -> u8`, which is configurable at init time via the `decimals: Option<u8>` field of `InitArgs`. When `None`, it defaults to 8, but any value from 0 to 255 is valid: [3](#0-2) [4](#0-3) 

The function is called for SNS tokens via `try_get_sns_token_balance_valuation`, which passes the SNS ledger canister as the `icrc1_client` but never fetches its actual decimal count: [5](#0-4) 

The resulting `ValuationFactors.tokens` value feeds directly into `ValuationFactors::to_xdr()`: [6](#0-5) 

This XDR valuation is then used by `ProposalsAmountTotalUpperBound` to compute the 7-day spending cap for `TransferSnsTreasuryFunds` proposals: [7](#0-6) 

And enforced at execution time in `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err`: [8](#0-7) 

### Impact Explanation

| SNS token decimals | Divisor used | Correct divisor | Effect on `tokens` | Effect on spending cap |
|---|---|---|---|---|
| 6 (e.g. USDC-like) | 10^8 | 10^6 | 100× too small | Cap is 100× too tight — legitimate transfers blocked |
| 18 (e.g. ETH-like) | 10^8 | 10^18 | 10^10× too large | Cap is 10^10× too loose — treasury can be drained in a single proposal |

For an SNS token with 18 decimals, the computed treasury value in XDR is inflated by a factor of 10^10. The `ProposalsAmountTotalUpperBound` would then permit a single `TransferSnsTreasuryFunds` proposal to move the entire treasury (or far more than the intended 3% / 10% / 100% fraction), bypassing the economic safety guard entirely.

### Likelihood Explanation

The ICRC-1 standard explicitly supports configurable decimals. Any SNS that initializes its ledger with `decimals` set to a value other than 8 — which is a valid and documented option — will silently trigger this miscalculation. The SNS framework does not enforce 8 decimals. An SNS community or developer could deploy with 18 decimals (common in EVM-compatible designs) and the governance spending guard would be rendered ineffective. The entry path is a standard governance proposal submission, requiring no privileged access.

### Recommendation

Replace the hardcoded `E8` divisor with a dynamic fetch of `icrc1_decimals` from the ledger canister. Concretely, `try_get_balance_valuation_factors` should call `icrc1_decimals` on the `icrc1_client` (alongside the existing `icrc1_balance_of` call) and use `10_u128.pow(decimals as u32)` as the divisor. The `Icrc1Client` trait should be extended with a `icrc1_decimals` method, and `LedgerCanister` should implement it by calling the `"icrc1_decimals"` endpoint.

### Proof of Concept

1. Deploy an SNS whose ledger is initialized with `decimals: Some(18)`.
2. Fund the SNS treasury with 1 SNS token (raw value: `1_000_000_000_000_000_000`).
3. Submit a `TransferSnsTreasuryFunds` proposal for the full treasury amount.
4. At proposal creation, `try_get_balance_valuation_factors` computes:
   - `balance_of_response.0 = 1_000_000_000_000_000_000`
   - `tokens = 1_000_000_000_000_000_000 / 100_000_000 = 10_000_000_000` (10 billion "tokens" instead of 1)
5. The XDR valuation is inflated by 10^10, so the 7-day cap is also inflated by 10^10.
6. The proposal passes the spending-limit check and executes, draining the treasury in a single governance action — bypassing the intended economic safety guard.

### Citations

**File:** rs/sns/governance/token_valuation/src/lib.rs (L37-57)
```rust
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

**File:** rs/sns/governance/token_valuation/src/lib.rs (L117-126)
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
```

**File:** rs/sns/governance/token_valuation/src/lib.rs (L177-181)
```rust
    let tokens = Decimal::from(u128::try_from(balance_of_response.0).map_err(|err| {
        ValuationError::new_arithmetic(format!(
            "Balance of {account:?} does not fit in u128: {err:?}"
        ))
    })?) / Decimal::from(E8);
```

**File:** rs/nervous_system/common/src/lib.rs (L192-192)
```rust
/// A more convenient (but explosive) way to do token math. Not suitable for
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L513-516)
```rust
#[query]
fn icrc1_decimals() -> u8 {
    Access::with_ledger(|ledger| ledger.decimals())
}
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L206-209)
```rust
    pub fn with_decimals(mut self, decimals: u8) -> Self {
        self.0.decimals = Some(decimals);
        self
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
