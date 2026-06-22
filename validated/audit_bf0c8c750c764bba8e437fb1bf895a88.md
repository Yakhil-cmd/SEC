### Title
SNS Treasury Valuation Uses Uncapped `icps_per_token` Spot Price, Enabling Governance-Controlled Price Inflation to Bypass 7-Day Transfer Limits - (File: rs/sns/governance/token_valuation/src/lib.rs)

### Summary
The SNS governance treasury-transfer and token-minting rate-limiting system computes a treasury valuation at proposal submission time using a spot-derived `icps_per_token` price with no upper-bound cap. The `xdrs_per_icp` dimension has a `MIN_XDRS_PER_ICP` floor clamp, but the code explicitly documents that no `MAX_XDRS_PER_ICP` is enforced, and — critically — `icps_per_token` has no clamp at all. An SNS governance majority (or a colluding proposer with sufficient voting power) can inflate the apparent SNS token price at proposal-submission time by manipulating the swap canister's `get_derived_state` response (e.g., by temporarily reducing the ICP participation denominator), causing the treasury to appear "small" (≤ 100,000 XDR) and thus fall into the `NoLimit` regime, bypassing the 7-day transfer cap entirely.

### Finding Description

The SNS governance canister enforces a 7-day rolling cap on `TransferSnsTreasuryFunds` and `MintSnsTokens` proposals via `ProposalsAmountTotalUpperBound::in_tokens`. The cap regime is determined by the treasury's XDR valuation at proposal-submission time:

- **Small** (≤ 100,000 XDR): `NoLimit` — the full treasury balance can be transferred.
- **Medium** (≤ 1,200,000 XDR): 25% of balance per 7 days.
- **Large** (> 1,200,000 XDR): 300,000 XDR worth per 7 days. [1](#0-0) 

The valuation is computed in `assess_treasury_balance` → `try_get_sns_token_balance_valuation` → `IcpsPerSnsTokenClient::fetch_icps_per_sns_token`. The `icps_per_token` factor is derived from the swap canister's `get_derived_state` response field `sns_tokens_per_icp`:

```
icps_per_sns_token = (1 / sns_tokens_per_icp) / (current_supply / initial_supply)
``` [2](#0-1) 

The `xdrs_per_icp` factor has a floor clamp (`MIN_XDRS_PER_ICP = 1`), but the code explicitly states no maximum is enforced:

> "Currently, we do not have/enforce a `MAX_XDRS_PER_ICP`, because this would tend to cause our valuations to be in the 'large' regime, where actions are more limited." [3](#0-2) 

Critically, `icps_per_token` has **no clamp at all** — neither floor nor ceiling. The `clamp_xdrs_per_icp` function only touches `xdrs_per_icp`: [4](#0-3) 

The `sns_tokens_per_icp` value in `DerivedState` is computed live from the swap canister's current state:

```rust
let sns_tokens_per_icp = i2d(tokens_available_for_swap)
    .checked_div(i2d(participant_total_icp_e8s))
    .and_then(|d| d.to_f32())
    .unwrap_or(0.0);
``` [5](#0-4) 

This means `sns_tokens_per_icp` is a **live spot ratio** that changes with every ICP deposit or withdrawal during an open swap. If the swap is still open (or if the SNS controls the swap canister), the ratio can be transiently manipulated.

The treasury valuation formula is:

```
XDR_value = tokens × icps_per_token × xdrs_per_icp
``` [6](#0-5) 

If `icps_per_token` is artificially **deflated** (by making `sns_tokens_per_icp` appear very large, i.e., very few ICP in the swap), the XDR valuation of the treasury drops below 100,000 XDR, triggering `NoLimit`, and the full treasury can be drained in a single proposal.

Conversely, the `NoLimit` branch returns `balance_tokens` directly — the entire treasury balance — as the allowed transfer amount: [7](#0-6) 

The valuation snapshot is taken at proposal-submission time and frozen into `TransferSnsTreasuryFundsActionAuxiliary`. At execution time, only the frozen valuation is re-used (not re-fetched), so the manipulation only needs to be active during the brief window of proposal submission. [8](#0-7) [9](#0-8) 

### Impact Explanation

An SNS governance majority (or a proposer who can pass a proposal) can drain the SNS treasury (ICP or SNS tokens) beyond the intended 7-day rolling cap by manipulating the `icps_per_token` spot price at proposal-submission time to push the treasury valuation into the `NoLimit` regime. For a large SNS treasury (e.g., worth 10M XDR), the normal cap is 300,000 XDR per 7 days. By deflating `icps_per_token` sufficiently, the attacker can make the treasury appear worth ≤ 100,000 XDR and transfer the entire balance in one proposal. This is a **ledger conservation / governance authorization bug**: the rate-limiting safety mechanism is bypassed, enabling unauthorized large-scale treasury extraction.

### Likelihood Explanation

The attack requires SNS governance majority to pass the `TransferSnsTreasuryFunds` proposal. However, the vulnerability is relevant precisely in adversarial governance scenarios (e.g., a governance takeover, a malicious founding team, or a whale with majority voting power). The swap canister's `get_derived_state` is a query/update callable by anyone, and the `sns_tokens_per_icp` value is a live spot ratio. During an open swap, any participant can transiently shift the ratio by depositing a large amount of ICP (reducing `sns_tokens_per_icp`, thus inflating `icps_per_token`... wait — the direction matters). More precisely: to **deflate** `icps_per_token`, the attacker needs `sns_tokens_per_icp` to be **large** (many tokens per ICP), which happens when `participant_total_icp_e8s` is small. If the swap is open with very little ICP participation, `sns_tokens_per_icp` is large, `icps_per_token` is small, and the treasury valuation is low. This is a realistic condition for a newly launched SNS swap. The attack is realistic for any SNS where the swap is still open or where the SNS controls its own swap canister.

### Recommendation

1. **Add a `MAX_ICPS_PER_TOKEN` clamp** in `ProposalsAmountTotalUpperBound::in_tokens` analogous to `MIN_XDRS_PER_ICP`, to prevent artificially low treasury valuations from triggering `NoLimit`.
2. **Add a `MAX_XDRS_PER_ICP` clamp** as well, since the code explicitly acknowledges this is missing. A high `xdrs_per_icp` inflates the valuation into the "large" regime (more restrictive), but a low `icps_per_token` deflates it into "small" (no limit).
3. Consider using a **time-weighted average price (TWAP)** for `icps_per_token` rather than the live spot ratio from `get_derived_state`, analogous to the 30-day moving average already used for `xdrs_per_icp` via `get_average_icp_xdr_conversion_rate`.
4. Consider re-fetching the valuation at execution time (not just at submission time) to prevent stale-valuation exploits.

### Proof of Concept

**Setup:** An SNS has a treasury worth 5,000,000 XDR (large regime, cap = 300,000 XDR/7 days). The swap is open with `sns_token_e8s = 1,000,000 * E8` and `participant_total_icp_e8s = 1 * E8` (1 ICP total participation — e.g., only the minimum has been contributed).

**Step 1:** `sns_tokens_per_icp = 1,000,000 / 1 = 1,000,000` (very large).

**Step 2:** `icps_per_sns_token = 1 / 1,000,000 = 0.000001` ICP per token.

**Step 3:** Treasury valuation = `treasury_tokens × 0.000001 × xdrs_per_icp`. If `xdrs_per_icp = 10` and `treasury_tokens = 10,000,000`, then XDR value = `10,000,000 × 0.000001 × 10 = 100 XDR`.

**Step 4:** `100 XDR ≤ MAX_SMALL_TREASURY_SIZE_XDR (100,000)` → `NoLimit` regime.

**Step 5:** Attacker submits `TransferSnsTreasuryFunds` for the full treasury balance. The valuation snapshot records `NoLimit`. The proposal passes governance vote.

**Step 6:** At execution time, `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` uses the **frozen** valuation (still `NoLimit`) and allows the full transfer. [10](#0-9) 

The attacker has bypassed the 300,000 XDR/7-day cap and drained the entire treasury.

### Citations

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L36-41)
```rust
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

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L76-77)
```rust
        let result_tokens = match self_ {
            Self::NoLimit => balance_tokens,
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L137-140)
```rust
    fn clamp_xdrs_per_icp(valuation: &mut Valuation) {
        let xdrs_per_icp = &mut valuation.valuation_factors.xdrs_per_icp;
        *xdrs_per_icp = (*xdrs_per_icp).max(Self::MIN_XDRS_PER_ICP);
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

**File:** rs/sns/swap/src/swap.rs (L2992-2995)
```rust
        let sns_tokens_per_icp = i2d(tokens_available_for_swap)
            .checked_div(i2d(participant_total_icp_e8s))
            .and_then(|d| d.to_f32())
            .unwrap_or(0.0);
```

**File:** rs/sns/governance/src/proposal.rs (L570-578)
```rust
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

**File:** rs/sns/governance/src/governance.rs (L3000-3005)
```rust
        transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err(
            transfer,
            valuation?,
            self.proto.proposals.values(),
            self.env.now(),
        )?;
```
