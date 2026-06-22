### Title
SNS Token Treasury Valuation Uses Single Hardcoded Price Path (Swap-Derived Initial Price), Causing Incorrect Treasury Transfer Limits - (File: `rs/sns/governance/token_valuation/src/lib.rs`)

---

### Summary

The `IcpsPerSnsTokenClient::fetch_icps_per_sns_token` function computes the SNS token price using only one hardcoded path: the initial swap price adjusted for total supply inflation. It never consults a current market price. This is the direct IC analog of `getUsdPrice()` only supporting direct USDT pools or token-WETH-USDT paths. The resulting stale valuation is used to enforce the 7-day treasury transfer upper bound on `TransferSnsTreasuryFunds` proposals. When the calculated price is sufficiently low, the treasury is classified as "small" and the limit degrades to `NoLimit`, removing all transfer restrictions.

---

### Finding Description

`fetch_icps_per_sns_token` in `IcpsPerSnsTokenClient` derives the current SNS token price via a single fixed path:

1. Fetch `sns_tokens_per_icp` from the swap canister's `get_derived_state` (the price frozen at swap finalization).
2. Compute `initial_icps_per_sns_token = 1 / sns_tokens_per_icp`.
3. Compute `total_inflation = current_supply / initial_supply`.
4. Return `initial_icps_per_sns_token / total_inflation`. [1](#0-0) 

No alternative price path (e.g., a DEX spot price or an external oracle) is ever consulted. The comment in the swap proto explicitly warns: *"Note that this should not be used for super precise financial accounting, because this is floating point."* [2](#0-1) 

The resulting `Valuation` is passed to `transfer_sns_treasury_funds_7_day_total_upper_bound_tokens`, which classifies the treasury as small/medium/large based on its XDR value and sets the transfer limit accordingly: [3](#0-2) 

If the computed `icps_per_token` is very small (e.g., SNS launched with a very low ICP price and has since inflated its supply), the treasury XDR value falls below `MAX_SMALL_TREASURY_SIZE_XDR` (100,000 XDR), triggering the `NoLimit` branch — **no cap on the transfer amount at all**.

The valuation is computed at proposal submission time and stored in `ActionAuxiliary::TransferSnsTreasuryFunds(valuation)`. The execution-time check re-uses this stored valuation: [4](#0-3) 

The full call chain is:

`validate_and_render_transfer_sns_treasury_funds` → `treasury_valuation_if_proposal_amount_is_small_enough_or_err` → `assess_treasury_balance` → `Token::assess_balance` → `try_get_sns_token_balance_valuation` → `IcpsPerSnsTokenClient::fetch_icps_per_sns_token` [5](#0-4) [6](#0-5) 

---

### Impact Explanation

**Scenario A — Token appreciated since swap (limit too permissive in token terms):**
If the SNS token's actual market price is 10× the swap-derived price, the XDR-denominated limit of 300,000 XDR translates to 10× more tokens than intended. Governance participants who rely on the displayed valuation are misled about the real XDR value being transferred.

**Scenario B — Token inflated / low initial price (NoLimit triggered):**
If `initial_sns_tokens_per_icp` was large (cheap token at swap) and the supply has since doubled or tripled, `icps_per_token` becomes tiny. The treasury XDR value drops below 100,000 XDR, the `NoLimit` branch fires, and the entire SNS token treasury can be proposed for transfer in a single proposal with no amount validation. [7](#0-6) 

The `MIN_XDRS_PER_ICP` floor of `1` protects the ICP→XDR leg, but there is **no corresponding floor for `icps_per_token`**, leaving the SNS token leg unprotected. [8](#0-7) 

---

### Likelihood Explanation

**Medium.** SNS tokens routinely experience large price movements after their initialization swap. An SNS that launched with a high `sns_tokens_per_icp` ratio (cheap token) and has since minted governance rewards will have a calculated `icps_per_token` far below the actual market rate. Any neuron holder can submit a `TransferSnsTreasuryFunds` proposal; if the computed valuation triggers `NoLimit`, the proposal passes the amount check unconditionally at submission time. Honest governance voters who trust the system's displayed valuation may approve it without realizing the safety limit has been silently disabled.

---

### Recommendation

1. **Add a floor for `icps_per_token`** analogous to `MIN_XDRS_PER_ICP`, so that an unrealistically low calculated price cannot collapse the treasury XDR value to zero and trigger `NoLimit`.
2. **Supplement the swap-derived price** with a secondary price source (e.g., a time-weighted average from an on-chain DEX or the XRC canister if the SNS token is listed) and use the higher of the two values when computing the treasury limit, to prevent undervaluation.
3. **Display the computed `icps_per_token` and resulting XDR valuation** in the proposal rendering so governance participants can detect a stale or anomalous price before voting.

---

### Proof of Concept

1. Deploy an SNS with `sns_token_e8s = 1_000_000_000 * E8` and `buyer_total_icp_e8s = 1_000 * E8`, giving `sns_tokens_per_icp ≈ 1_000_000`. Thus `initial_icps_per_sns_token ≈ 0.000001`.
2. Over time, governance minting doubles the supply: `current_supply = 2 * initial_supply`, so `total_inflation = 2`.
3. `fetch_icps_per_sns_token` returns `0.000001 / 2 = 0.0000005` ICP per SNS token.
4. With `xdrs_per_icp = 5`, `xdrs_per_token = 0.0000025`. A treasury of 10,000 SNS tokens is valued at `10,000 * 0.0000025 = 0.025 XDR` — far below 100,000 XDR.
5. `ProposalsAmountTotalUpperBound::from_valuation_xdr(0.025 XDR)` returns `NoLimit`.
6. A neuron holder submits `TransferSnsTreasuryFunds { amount_e8s: 10_000 * E8, from_treasury: SnsTokenTreasury, ... }`. The amount check passes unconditionally. If governance approves, the entire SNS token treasury is transferred. [9](#0-8) [10](#0-9)

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

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L818-821)
```text
  // Current approximate rate SNS tokens per ICP. Note that this should not be used for super
  // precise financial accounting, because this is floating point.
  float sns_tokens_per_icp = 2;
  // Current amount of contributions from direct swap participants.
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

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L64-64)
```rust
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

**File:** rs/sns/governance/src/proposal.rs (L770-817)
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
}
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

**File:** rs/sns/governance/src/treasury.rs (L256-270)
```rust
pub(crate) async fn assess_treasury_balance(
    token: Token,
    sns_governance_canister_id: CanisterId,
    sns_ledger_canister_id: CanisterId,
    swap_canister_id: CanisterId,
) -> Result<Valuation, String> {
    let treasury_account = token.treasury_account(sns_governance_canister_id)?;
    let valuation = token
        .assess_balance(sns_ledger_canister_id, swap_canister_id, treasury_account)
        .await
        .map_err(|valuation_error| {
            format!("Unable to assess current treasury balance: {valuation_error:?}")
        })?;
    Ok(valuation)
}
```
