### Title
Missing `icps_per_token` Floor Guard Allows SNS Treasury-Limit Silent Bypass via Bad Swap-Canister Read — (`rs/sns/governance/proposals_amount_total_limit/src/lib.rs`)

### Summary

The SNS treasury transfer/mint limit guard clamps `xdrs_per_icp` to prevent a bad CMC read from pushing the treasury into the "small" (NoLimit) regime, but applies no analogous floor to `icps_per_token`, which is fetched from the swap canister. A bad read of `icps_per_token` can silently collapse the treasury's XDR valuation below the 100,000 XDR threshold, activating the `NoLimit` variant and allowing the entire SNS treasury to be drained in a single governance proposal.

### Finding Description

`ProposalsAmountTotalUpperBound::in_tokens()` computes the treasury's XDR valuation as:

```
total_xdr = tokens × icps_per_token × xdrs_per_icp
``` [1](#0-0) 

Before computing `total_xdr`, the function calls `clamp_xdrs_per_icp()`, which enforces a minimum of `MIN_XDRS_PER_ICP = 1` on the CMC-sourced factor. The motivation is explicitly documented:

> "Low XDRs per ICP quotes would tend to cause our valuations to be in the 'small' regime, where an SNS is allowed to take the biggest actions relative to their size. This is to minimize the damage caused by wacky price quotes." [2](#0-1) 

No analogous floor is applied to `icps_per_token`. This value is fetched from the swap canister via `IcpsPerSnsTokenClient::fetch_icps_per_sns_token()`, which concurrently calls:

1. `swap_canister.get_derived_state()` → `sns_tokens_per_icp` (f64)
2. `sns_ledger.get_transactions()` → `initial_supply_e8s`
3. `sns_ledger.icrc1_total_supply()` → `current_supply_e8s` [3](#0-2) 

The formula is:

```
icps_per_sns_token = (1 / initial_sns_tokens_per_icp) / (current_supply_e8s / initial_supply_e8s)
``` [4](#0-3) 

If the swap canister returns an abnormally large `sns_tokens_per_icp` (e.g., due to a bug or edge case), `icps_per_token` collapses toward zero. With `xdrs_per_icp` clamped to 1, the total XDR valuation becomes:

```
total_xdr ≈ tokens × ε × 1  →  near zero
```

This falls below `MAX_SMALL_TREASURY_SIZE_XDR = 100,000`, activating the `NoLimit` branch: [5](#0-4) 

In the `NoLimit` regime, `in_tokens()` returns `balance_tokens` — the entire treasury balance — as the allowed amount. The guard in `treasury_valuation_if_proposal_amount_is_small_enough_or_err` then permits any proposal amount up to the full treasury: [6](#0-5) 

The same guard is used for both `TransferSnsTreasuryFunds` and `MintSnsTokens` proposals: [7](#0-6) [8](#0-7) 

The execution-time re-check (`transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err`) reuses the **same valuation snapshot** stored at proposal submission time, so a bad read at submission time propagates through to execution: [9](#0-8) 

### Impact Explanation

**Impact: Medium.** If the guard is bypassed, a governance proposal can transfer or mint the entire SNS treasury in a single action, bypassing the intended 7-day rolling limit. For a large SNS treasury this represents a complete loss of treasury funds. The bypass is silent — no error is raised; the proposal simply passes validation with `NoLimit` applied.

### Likelihood Explanation

**Likelihood: Medium.** The `icps_per_token` computation involves three concurrent inter-canister calls. Any of the following realistic conditions triggers the bypass:

1. **Swap canister bug:** `get_derived_state` returns an abnormally large `sns_tokens_per_icp` (e.g., due to a floating-point edge case or state corruption). The field is an `Option<f64>`, and `f64::MAX` or `f64::INFINITY` would not be caught by the existing `None` check.
2. **Massive token inflation:** If the SNS mints tokens aggressively over time (through legitimate governance), `current_supply_e8s / initial_supply_e8s` grows large, shrinking `icps_per_token` proportionally. For a treasury holding 1,000,000 SNS tokens at an initial price of 1 ICP/token, a 10× supply inflation yields `icps_per_token = 0.1`, placing the treasury exactly at the 100,000 XDR boundary. Further inflation silently activates `NoLimit`.
3. **Ledger `initial_supply_e8s` misread:** The `initial_supply_e8s` function reads the first mint transaction from the ledger. An edge case (e.g., archived transactions, reordering) returning a smaller-than-actual initial supply inflates the computed inflation ratio, deflating `icps_per_token`.

Unlike `xdrs_per_icp`, which is explicitly guarded, `icps_per_token` has no floor, making the guard asymmetric and incomplete — exactly the pattern described in the reference report.

### Recommendation

Add a `clamp_icps_per_token` function mirroring `clamp_xdrs_per_icp`, enforcing a minimum `MIN_ICPS_PER_TOKEN` on the swap-canister-sourced factor before computing `total_xdr`. The minimum value should be chosen conservatively (e.g., based on the lowest historically observed SNS token price in ICP). This guards each individual component of the valuation product, rather than relying on a single aggregate check. [10](#0-9) 

### Proof of Concept

1. SNS treasury holds 10,000,000 SNS tokens (a large treasury).
2. Initial swap price: `sns_tokens_per_icp = 1.0` → `initial_icps_per_sns_token = 1.0`.
3. Swap canister returns a bad `sns_tokens_per_icp = 1e7` (e.g., due to a float edge case).
4. `icps_per_token = 1 / 1e7 = 1e-7`.
5. `xdrs_per_icp` clamped to 1.
6. `total_xdr = 10,000,000 × 1e-7 × 1 = 1 XDR`.
7. `1 XDR < 100,000 XDR` → `NoLimit` branch selected.
8. `in_tokens()` returns `10,000,000` tokens as the allowed amount.
9. A `TransferSnsTreasuryFunds` proposal for the full 10,000,000 tokens passes submission-time validation.
10. Upon adoption, the entire treasury is transferred to the attacker's account.

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

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L137-141)
```rust
    fn clamp_xdrs_per_icp(valuation: &mut Valuation) {
        let xdrs_per_icp = &mut valuation.valuation_factors.xdrs_per_icp;
        *xdrs_per_icp = (*xdrs_per_icp).max(Self::MIN_XDRS_PER_ICP);
    }
}
```

**File:** rs/sns/governance/token_valuation/src/lib.rs (L314-330)
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
```

**File:** rs/sns/governance/token_valuation/src/lib.rs (L386-414)
```rust
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
```

**File:** rs/sns/governance/src/proposal.rs (L551-578)
```rust
/// Validates and render TransferSnsTreasuryFunds proposal
///
/// Returns ActionAuxiliary::TransferSnsTreasuryFunds.
async fn validate_and_render_transfer_sns_treasury_funds(
    transfer: &TransferSnsTreasuryFunds,
    sns_transfer_fee_e8s: u64,
    env: &dyn Environment,
    swap_canister_id: CanisterId,
    sns_ledger_canister_id: CanisterId,
    proposals: impl Iterator<Item = &ProposalData>,
) -> Result<
    (
        String, // Rendering.
        ActionAuxiliary,
    ),
    String,
> {
    let mut defects = vec![];

    // Validate amount. This requires calling CMC and the swap canister; hence, await.
    let valuation = treasury_valuation_if_proposal_amount_is_small_enough_or_err(
        env,
        sns_ledger_canister_id,
        swap_canister_id,
        proposals,
        transfer,
    )
    .await;
```

**File:** rs/sns/governance/src/proposal.rs (L799-814)
```rust
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
```

**File:** rs/sns/governance/src/proposal.rs (L872-899)
```rust
/// Validates and render MintSnsTokens proposal.
///
/// Returns ActionAuxiliary::MintSnsTokens.
async fn validate_and_render_mint_sns_tokens(
    mint_sns_tokens: &MintSnsTokens,
    sns_transfer_fee_e8s: u64,
    env: &dyn Environment,
    swap_canister_id: CanisterId,
    sns_ledger_canister_id: CanisterId,
    proposals: impl Iterator<Item = &ProposalData>,
) -> Result<
    (
        String, // Rendering.
        ActionAuxiliary,
    ),
    String,
> {
    let mut defects = vec![];

    // Validate amount. (This requires calling CMC and the swap canister; hence, await.)
    let valuation = treasury_valuation_if_proposal_amount_is_small_enough_or_err(
        env,
        sns_ledger_canister_id,
        swap_canister_id,
        proposals,
        mint_sns_tokens,
    )
    .await;
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
