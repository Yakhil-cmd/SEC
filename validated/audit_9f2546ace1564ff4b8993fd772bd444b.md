### Title
SNS Token Supply Deflation via Unrestricted Burn Inflates Treasury Valuation, Restricting Governance Treasury Transfers - (File: rs/sns/governance/token_valuation/src/lib.rs)

### Summary
Any unprivileged SNS token holder can burn their own tokens via the standard `icrc1_transfer` to the minting account. Because the SNS governance's treasury valuation formula computes the current token price as `initial_price / (current_supply / initial_supply)`, burning tokens deflates `current_supply`, inflates the computed price, and can push the treasury into a more restrictive 7-day transfer regime — reducing or capping the amount that `TransferSnsTreasuryFunds` proposals are allowed to execute.

### Finding Description

`IcpsPerSnsTokenClient::fetch_icps_per_sns_token()` computes the current SNS token price using three live on-chain values:

```
total_inflation = current_supply / initial_supply
current_price   = initial_icps_per_sns_token / total_inflation
``` [1](#0-0) 

When `current_supply` is reduced below `initial_supply` (deflation), `total_inflation < 1`, so `current_price > initial_price`. The treasury valuation in XDR is then `treasury_balance × current_price × xdrs_per_icp`. An inflated price inflates the XDR valuation, which determines the 7-day transfer limit regime: [2](#0-1) 

- **Small** (< 100,000 XDR): `NoLimit` — 100% of treasury can be transferred
- **Medium** (100,000–1,200,000 XDR): `Fraction(0.25)` — only 25% per 7 days
- **Large** (> 1,200,000 XDR): `Xdr(300,000)` — fixed XDR cap per 7 days [3](#0-2) 

The burn path is unrestricted for any token holder. The ICRC-1 ledger allows any account to burn by transferring to the minting account, with the only restriction being a minimum burn amount of `min(transfer_fee, balance)`: [4](#0-3) 

The valuation is consumed at proposal submission time and again at execution time: [5](#0-4) [6](#0-5) 

There is a `MIN_XDRS_PER_ICP` clamp to prevent artificially low price quotes from expanding the limit, but there is **no `MAX_XDRS_PER_ICP` clamp** — the code explicitly notes this: [7](#0-6) 

### Impact Explanation

An attacker who holds SNS tokens can burn a significant fraction of the circulating supply. This deflates `current_supply`, inflates `total_inflation`'s denominator, and raises `current_price`. If the SNS treasury was previously in the "small" regime (no transfer limit), the inflated valuation can push it into "medium" (25% cap) or "large" (fixed XDR cap), blocking or severely throttling `TransferSnsTreasuryFunds` governance proposals. This is a governance denial-of-service: legitimate SNS proposals that were valid at submission may fail at execution, or may be blocked from submission entirely.

### Likelihood Explanation

Likelihood is **low**. The attacker must own and burn a substantial fraction of the total SNS token supply to meaningfully inflate the price. This is economically costly to the attacker (they destroy their own tokens). Additionally, the effect is partially self-correcting over time as governance mints new tokens via voting rewards. The attack is most plausible for SNS instances with a small circulating supply and a treasury near the small/medium boundary.

### Recommendation

1. Apply a `MAX_XDRS_PER_ICP` clamp symmetrically with `MIN_XDRS_PER_ICP` in `ProposalsAmountTotalUpperBound::clamp_xdrs_per_icp` to bound the effective price used in treasury limit calculations.
2. Alternatively, cap `total_inflation` from below at a minimum value (e.g., `0.5`) so that deflation cannot inflate the computed price beyond a factor of 2× the initial price.
3. Consider using a time-weighted average supply rather than the instantaneous `icrc1_total_supply` to make the price resistant to short-term supply manipulation.

### Proof of Concept

Assume an SNS with:
- `initial_supply = 10,000,000 e8s` (100 tokens)
- `initial_sns_tokens_per_icp = 10` → `initial_icps_per_sns_token = 0.1`
- `xdrs_per_icp = 10`
- Treasury holds 500 SNS tokens → initial valuation = `500 × 0.1 × 10 = 500 XDR` (small, no limit)

Attacker burns 90% of circulating supply (9,000,000 e8s):
- `current_supply = 1,000,000 e8s`
- `total_inflation = 1,000,000 / 10,000,000 = 0.1`
- `current_price = 0.1 / 0.1 = 1.0 ICP per token`
- Treasury valuation = `500 × 1.0 × 10 = 5,000 XDR` (still small, but price inflated 10×)

For a larger treasury (e.g., 5,000 tokens initially worth 50,000 XDR, near the small/medium boundary), the same burn pushes valuation to 500,000 XDR (medium regime), imposing a 25% per-7-day cap where none existed before. Any pending `TransferSnsTreasuryFunds` proposal for more than 25% of the treasury would then fail at execution via `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err`. [8](#0-7)

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

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L60-64)
```rust
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

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L606-635)
```rust
        let (tx, effective_fee) = if &to == ledger.minting_account() {
            let expected_fee = Tokens::zero();
            if fee.is_some() && fee.as_ref() != Some(&expected_fee.into()) {
                return Err(CoreTransferError::BadFee { expected_fee });
            }

            let balance = ledger.balances().account_balance(&from_account);
            let min_burn_amount = ledger.transfer_fee().min(balance);
            if amount < min_burn_amount {
                return Err(CoreTransferError::BadBurn { min_burn_amount });
            }
            if Tokens::is_zero(&amount) {
                return Err(CoreTransferError::BadBurn {
                    min_burn_amount: ledger.transfer_fee(),
                });
            }

            (
                Transaction {
                    operation: Operation::Burn {
                        from: from_account,
                        spender,
                        amount,
                        fee: None,
                    },
                    created_at_time: created_at_time.map(|t| t.as_nanos_since_unix_epoch()),
                    memo,
                },
                Tokens::zero(),
            )
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
