### Title
`MintSnsTokens` 7-Day Minting Cap Bypassed via `Decimal::MAX` Upper Bound — (File: rs/sns/governance/src/proposal.rs)

---

### Summary

The `MintSnsTokens` governance proposal action's implementation of `recent_amount_total_upper_bound_tokens` unconditionally returns `Decimal::MAX`, rendering the 7-day treasury-based minting limit a dead letter. The validation framework computes a cap and checks the proposal amount against it, but because the cap is always `Decimal::MAX`, the check never rejects any amount. Any `MintSnsTokens` proposal that achieves a governance vote can mint an unbounded quantity of SNS tokens, bypassing the intended 25%-of-treasury-per-7-days safety invariant.

---

### Finding Description

`rs/sns/governance/src/proposal.rs` defines the `TokenProposalAction` trait and a shared validation helper `treasury_valuation_if_proposal_amount_is_small_enough_or_err` (lines 770–817). This helper:

1. Calls `action.recent_amount_total_tokens(proposals, env.now())` to sum recently executed minting proposals.
2. Fetches the treasury valuation.
3. Calls `MyTokenProposalAction::recent_amount_total_upper_bound_tokens(&valuation)` to obtain `max_tokens`.
4. Checks `proposal_amount_tokens > allowance_remainder_tokens` (where `allowance_remainder_tokens = max_tokens - spent_tokens`). [1](#0-0) 

For `TransferSnsTreasuryFunds`, the bound is correctly derived from the treasury valuation. For `MintSnsTokens`, however, the active implementation is:

```rust
// TODO(NNS1-2982): Delete.
fn recent_amount_total_upper_bound_tokens(_valuation: &Valuation) -> Result<Decimal, String> {
    Ok(Decimal::MAX)
}
``` [2](#0-1) 

Because `max_tokens = Decimal::MAX`, `allowance_remainder_tokens` is always astronomically large, and the guard at line 805 (`proposal_amount_tokens > allowance_remainder_tokens`) can never be true. The check is structurally present but functionally inert — an exact analog to the Beanstalk pattern where a capped variable is computed but the original uncapped value is forwarded to subsequent operations.

The correct implementation is commented out directly above:

```rust
/* TODO(NNS1-2982): Uncomment.
fn recent_amount_total_upper_bound_tokens(valuation: &Valuation) -> Result<Decimal, String> {
    mint_sns_tokens_7_day_total_upper_bound_tokens(*valuation)
        ...
}
*/
``` [3](#0-2) 

`mint_sns_tokens_7_day_total_upper_bound_tokens` is fully implemented and would enforce the same 25%-of-treasury-per-7-days ceiling that `TransferSnsTreasuryFunds` already enforces: [4](#0-3) 

The `total_minting_amount_tokens` accumulator that feeds `spent_tokens` is also fully implemented but marked `#[allow(unused)]`: [5](#0-4) 

---

### Impact Explanation

Any SNS neuron holder who can assemble a passing governance vote on a `MintSnsTokens` proposal faces **no protocol-enforced ceiling** on the minted amount. A single proposal can mint tokens equal to or exceeding the entire treasury value, arbitrarily diluting all existing SNS token holders and collapsing the token price. The 7-day rolling window that is supposed to bound this action to ≤25% of treasury value is completely inoperative for `MintSnsTokens`.

---

### Likelihood Explanation

Exploitation requires achieving a governance majority on a `MintSnsTokens` proposal. In many SNS deployments, voting power is concentrated among a small number of early participants or the founding team, making a supermajority achievable by a single large holder or a small coalition. The entry path is a standard ingress call to `manage_neuron` (submit proposal) followed by voting — no privileged key or subnet-level access is required. The vulnerability is reachable by any SNS governance participant.

---

### Recommendation

**Short term:** Uncomment the real `recent_amount_total_upper_bound_tokens` implementation (lines 1025–1033) and delete the `Decimal::MAX` placeholder (lines 1035–1041). Resolve ticket NNS1-2982 before the `MintSnsTokens` proposal type is used in production SNS deployments.

**Long term:** Add an integration test that submits a `MintSnsTokens` proposal whose amount exceeds the 7-day treasury cap and asserts that proposal validation rejects it. Document the invariant "no single 7-day window may mint more than 25% of treasury value" in the SNS governance spec.

---

### Proof of Concept

1. An SNS neuron holder calls `manage_neuron` with a `MintSnsTokens` action specifying `amount_e8s` equal to, say, 10× the total SNS token supply.
2. The proposal enters voting; a governance majority approves it.
3. `validate_and_render_mint_sns_tokens` is called, which calls `treasury_valuation_if_proposal_amount_is_small_enough_or_err`.
4. Inside that helper, `MintSnsTokens::recent_amount_total_upper_bound_tokens` returns `Decimal::MAX`.
5. `allowance_remainder_tokens = Decimal::MAX - spent_tokens` is still effectively `Decimal::MAX`.
6. The guard `proposal_amount_tokens > allowance_remainder_tokens` evaluates to `false`; validation passes.
7. The proposal executes, minting 10× the token supply into the target account, collapsing the token value for all other holders. [6](#0-5) [2](#0-1)

### Citations

**File:** rs/sns/governance/src/proposal.rs (L792-814)
```rust
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
```

**File:** rs/sns/governance/src/proposal.rs (L872-930)
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
    let valuation = match valuation {
        Ok(ok) => Some(ok),
        Err(err) => {
            defects.push(err);
            None
        }
    };

    locally_validate_and_render_mint_sns_tokens(mint_sns_tokens, sns_transfer_fee_e8s, defects)
        .and_then(|rendering| {
            match valuation {
                Some(valuation) => Ok((rendering, ActionAuxiliary::MintSnsTokens(valuation))),

                // Proof that this never happens:
                //
                //   1. valuation = None means that amount_result was Err.
                //
                //   2. In that case, nonempty defects was passed to
                //      locally_validate_and_render_mint_sns_tokens.
                //
                //   3. In that case, the function always returns Err.
                //
                //   4. Then, this closure doesn't get called.
                None => Err(
                    "There is a bug in the amount validator. Somehow, no valuation, \
                     even though a rendering was generated."
                        .to_string(),
                ),
            }
        })
}
```

**File:** rs/sns/governance/src/proposal.rs (L1025-1033)
```rust
    /* TODO(NNS1-2982): Uncomment.
    fn recent_amount_total_upper_bound_tokens(valuation: &Valuation) -> Result<Decimal, String> {
        mint_sns_tokens_7_day_total_upper_bound_tokens(*valuation)
            // Err is most likely a bug.
            .map_err(|treasury_limit_error| {
                format!("Unable to validate amount: {:?}", treasury_limit_error,)
            })
    }
    */
```

**File:** rs/sns/governance/src/proposal.rs (L1035-1041)
```rust
    // TODO(NNS1-2982): Delete.
    fn recent_amount_total_upper_bound_tokens(_valuation: &Valuation) -> Result<Decimal, String> {
        // Ideally, we'd return infinity, but Decimal does not have that. This is the next best
        // thing, and should be good enough, because we have already planned the obselences of this
        // code (see tickets NNS1-298(1|2)).
        Ok(Decimal::MAX)
    }
```

**File:** rs/sns/governance/src/proposal.rs (L2707-2728)
```rust
#[allow(unused)] // TODO(NNS1-2910): Delete this.
fn total_minting_amount_tokens<'a>(
    proposals: impl Iterator<Item = &'a ProposalData>,
    min_executed_timestamp_seconds: u64,
) -> Result<Decimal, String> {
    let filter_proposal_action_amount_e8s = |action: &Action| {
        let mint = match action {
            Action::MintSnsTokens(ok) => ok,
            // Skip other types of proposals.
            _ => return None,
        };

        mint.amount_e8s
    };

    total_proposal_amounts_tokens(
        proposals,
        "MintSnsTokens",
        filter_proposal_action_amount_e8s,
        min_executed_timestamp_seconds,
    )
}
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L14-18)
```rust
pub fn mint_sns_tokens_7_day_total_upper_bound_tokens(
    valuation: Valuation,
) -> Result<Decimal, ProposalsAmountTotalLimitError> {
    ProposalsAmountTotalUpperBound::in_tokens(valuation)
}
```
