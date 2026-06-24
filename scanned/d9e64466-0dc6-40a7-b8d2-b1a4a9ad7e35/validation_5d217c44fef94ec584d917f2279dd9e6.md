### Title
Missing Upper Bound on `icps_per_token` in SNS Treasury Valuation Allows Inflated Spot-Price to Bypass 7-Day Transfer Limit - (`File: rs/sns/governance/proposals_amount_total_limit/src/lib.rs`)

---

### Summary

The SNS governance treasury-transfer limit system uses a single-point-in-time (spot) valuation of the SNS token price at proposal submission time. The `icps_per_token` factor — derived from the swap canister's `get_derived_state` `sns_tokens_per_icp` field — has no upper-bound clamp, unlike `xdrs_per_icp` which is floored at `MIN_XDRS_PER_ICP = 1`. An artificially inflated `icps_per_token` (e.g., from a temporarily depressed `buyer_total_icp_e8s` in the swap canister's live derived state) causes the treasury to appear large in XDR, pushing it into the "large" regime and capping the 7-day allowance at a fixed 300,000 XDR. When the token price is inflated, 300,000 XDR converts to a *smaller* number of tokens, meaning the attacker can drain more real value per proposal than the system intends to allow.

---

### Finding Description

**Price derivation path:**

1. `assess_treasury_balance` → `try_get_sns_token_balance_valuation` → `IcpsPerSnsTokenClient::fetch_icps_per_sns_token`
2. `fetch_icps_per_sns_token` calls `swap.get_derived_state()` to obtain `sns_tokens_per_icp` (a live, instantaneous ratio computed as `sns_token_e8s / participant_total_icp_e8s`)
3. This is inverted to `icps_per_token = 1 / sns_tokens_per_icp` and then adjusted for inflation: `icps_per_token / (current_supply / initial_supply)`

The `sns_tokens_per_icp` in `DerivedState` is computed live from the swap canister's current state:

```rust
// rs/sns/swap/src/swap.rs:2992-2995
let sns_tokens_per_icp = i2d(tokens_available_for_swap)
    .checked_div(i2d(participant_total_icp_e8s))
    .and_then(|d| d.to_f32())
    .unwrap_or(0.0);
```

This is a **spot price** — it reflects the current ratio of SNS tokens to ICP in the swap, not a time-weighted average.

**The limit logic in `ProposalsAmountTotalUpperBound::in_tokens`:**

- `xdrs_per_icp` is clamped to a minimum of 1 via `clamp_xdrs_per_icp`
- `icps_per_token` has **no corresponding maximum clamp**

The code explicitly acknowledges this asymmetry:

```rust
// rs/sns/governance/proposals_amount_total_limit/src/lib.rs:60-64
/// # Why Not Also Define MAX?
///
/// Currently, we do not have/enforce a MAX_XDRS_PER_ICP, because this would tend to cause our
/// valuations to be in the "large" regime, where actions are more limited.
const MIN_XDRS_PER_ICP: Decimal = dec!(1);
```

The comment explains the rationale for not capping `xdrs_per_icp` from above, but the same reasoning applies to `icps_per_token`: an inflated `icps_per_token` also pushes the treasury into the "large" regime, where the 300,000 XDR cap applies. When `icps_per_token` is inflated, 300,000 XDR converts to *fewer tokens*, so the per-proposal token allowance shrinks — but the **real value** of those tokens (at the true market price) may still be very large.

More critically, the inverse effect: if `icps_per_token` is **deflated** (e.g., by temporarily flooding the swap with ICP participation to depress the ratio), the treasury appears smaller in XDR, pushing it into the "small" or "medium" regime where the limit is `NoLimit` (100% of treasury) or 25% of treasury — allowing a much larger token drain.

**Attacker-controlled entry path:**

The swap canister's `get_derived_state` is a public query. The `sns_tokens_per_icp` value is computed from `participant_total_icp_e8s`, which is the live sum of all buyer ICP deposits. During an open swap, an attacker who controls a large ICP position can:
1. Temporarily increase `participant_total_icp_e8s` (by depositing ICP into the swap via `refresh_buyer_tokens`)
2. This deflates `sns_tokens_per_icp` → inflates `icps_per_token` → inflates treasury XDR valuation → pushes into "large" regime → 300,000 XDR cap applies → fewer tokens allowed per proposal

Or, if the swap is already finalized (committed/aborted), `sns_tokens_per_icp` is frozen at the final swap ratio and cannot be manipulated. However, for SNS tokens whose swap is still open or whose `get_derived_state` reflects a live state, the spot price is manipulable.

---

### Impact Explanation

**Governance authorization bug / ledger conservation bug.**

For an SNS with an open swap, an attacker (who is also an SNS neuron holder with proposal submission rights) can manipulate the live `sns_tokens_per_icp` ratio at the moment of proposal submission to shift the treasury into a different valuation regime. This can either:

- **Deflate the apparent treasury value** → push into "small" regime → `NoLimit` → drain 100% of treasury tokens in a single 7-day window instead of the intended 25% or fixed-XDR cap
- **Inflate the apparent treasury value** → push into "large" regime → the 300,000 XDR cap applies, but at an inflated token price, the actual token allowance is smaller than intended

The most dangerous scenario is deflation: if `icps_per_token` is driven near zero (by flooding the swap with ICP), the treasury XDR valuation drops below 100,000 XDR, triggering `NoLimit`, allowing the full treasury to be drained.

---

### Likelihood Explanation

- Requires the attacker to be an SNS neuron holder with proposal submission rights (unprivileged canister caller / governance user)
- Requires the SNS swap to still be in an open state (or for the swap canister to return a live `sns_tokens_per_icp`)
- The manipulation is a single cross-canister call timing attack at proposal submission — no flash loan infrastructure needed on IC (no atomic multi-step transactions), but the attacker must hold enough ICP to shift the ratio meaningfully
- The code comment explicitly acknowledges no `MAX_XDRS_PER_ICP` is enforced, and the same gap exists for `icps_per_token`

Likelihood: **Medium** — requires specific conditions (open swap, neuron holder), but the attack surface is real and the code explicitly lacks the symmetric bound.

---

### Recommendation

1. Add a `MAX_ICPS_PER_TOKEN` clamp in `ProposalsAmountTotalUpperBound::in_tokens` (analogous to `MIN_XDRS_PER_ICP`) to prevent inflated `icps_per_token` from pushing the treasury into an artificially large regime.
2. Add a `MIN_ICPS_PER_TOKEN` floor to prevent deflated `icps_per_token` from pushing the treasury into the "small/NoLimit" regime.
3. Consider using a time-averaged or finalized price for `icps_per_token` rather than the live swap `get_derived_state` spot price — for example, only use the finalized swap ratio once the swap is committed.

---

### Proof of Concept

**Root cause — no upper/lower bound on `icps_per_token`:** [1](#0-0) 

**The clamp only applies to `xdrs_per_icp`, not `icps_per_token`:** [2](#0-1) 

**`icps_per_token` is derived from the live swap spot price:** [3](#0-2) 

**The swap's `sns_tokens_per_icp` is a live instantaneous ratio, not time-averaged:** [4](#0-3) 

**The valuation directly determines the 7-day transfer limit regime:** [5](#0-4) 

**The valuation is fetched at proposal submission time and locked in:** [6](#0-5) 

**At execution time, the locked-in valuation (not a fresh one) is used:** [7](#0-6)

### Citations

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L60-64)
```rust
    /// # Why Not Also Define MAX?
    ///
    /// Currently, we do not have/enforce a MAX_XDRS_PER_ICP, because this would tend to cause our
    /// valuations to be in the "large" regime, where actions are more limited.
    const MIN_XDRS_PER_ICP: Decimal = dec!(1);
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

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L137-140)
```rust
    fn clamp_xdrs_per_icp(valuation: &mut Valuation) {
        let xdrs_per_icp = &mut valuation.valuation_factors.xdrs_per_icp;
        *xdrs_per_icp = (*xdrs_per_icp).max(Self::MIN_XDRS_PER_ICP);
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

**File:** rs/sns/swap/src/swap.rs (L2992-2995)
```rust
        let sns_tokens_per_icp = i2d(tokens_available_for_swap)
            .checked_div(i2d(participant_total_icp_e8s))
            .and_then(|d| d.to_f32())
            .unwrap_or(0.0);
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
