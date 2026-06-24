### Title
`MintSnsTokens` 7-Day Rolling Rate Limit Bypassed via Disabled `recent_amount_total_upper_bound_tokens` - (`rs/sns/governance/src/proposal.rs`)

---

### Summary

The SNS governance canister enforces a 7-day rolling cap on `TransferSnsTreasuryFunds` proposals but intentionally disables the equivalent cap for `MintSnsTokens` proposals. The `MintSnsTokens` implementation of `recent_amount_total_upper_bound_tokens` returns `Decimal::MAX` (unlimited), while the correct implementation calling `mint_sns_tokens_7_day_total_upper_bound_tokens` is commented out. This allows an SNS governance majority to pass unlimited `MintSnsTokens` proposals within any 7-day window, bypassing the rate-limit safeguard that is the primary protection against runaway token inflation via governance.

---

### Finding Description

The `TokenProposalAction` trait defines `recent_amount_total_upper_bound_tokens` as the mechanism that caps how many tokens can be minted or transferred within a rolling 7-day window.

For `TransferSnsTreasuryFunds`, the implementation is fully active:

```rust
fn recent_amount_total_upper_bound_tokens(valuation: &Valuation) -> Result<Decimal, String> {
    transfer_sns_treasury_funds_7_day_total_upper_bound_tokens(*valuation)
        .map_err(|treasury_limit_error| {
            format!("Unable to validate amount: {treasury_limit_error:?}",)
        })
}
``` [1](#0-0) 

For `MintSnsTokens`, the real implementation is commented out and replaced with a stub returning `Decimal::MAX`:

```rust
// TODO(NNS1-2982): Delete.
fn recent_amount_total_upper_bound_tokens(_valuation: &Valuation) -> Result<Decimal, String> {
    Ok(Decimal::MAX)
}
``` [2](#0-1) 

The correct implementation is blocked behind a comment:

```rust
/* TODO(NNS1-2982): Uncomment.
fn recent_amount_total_upper_bound_tokens(valuation: &Valuation) -> Result<Decimal, String> {
    mint_sns_tokens_7_day_total_upper_bound_tokens(*valuation)
        ...
}
*/
``` [3](#0-2) 

The import of `mint_sns_tokens_7_day_total_upper_bound_tokens` is also commented out at the top of the file:

```rust
use ic_sns_governance_proposals_amount_total_limit::{
    // TODO(NNS1-2982): Uncomment. mint_sns_tokens_7_day_total_upper_bound_tokens,
    transfer_sns_treasury_funds_7_day_total_upper_bound_tokens,
};
``` [4](#0-3) 

The rate-limit enforcement function `treasury_valuation_if_proposal_amount_is_small_enough_or_err` is called for both proposal types during validation, but because `MintSnsTokens::recent_amount_total_upper_bound_tokens` returns `Decimal::MAX`, the check at line 805 (`if proposal_amount_tokens > allowance_remainder_tokens`) can never trigger for minting proposals. [5](#0-4) 

Additionally, `TransferSnsTreasuryFunds` has a **second** execution-time check (`transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err`) that re-validates the cap at the moment of execution. No equivalent execution-time check exists for `MintSnsTokens`. [6](#0-5) 

The rate-limit library itself (`mint_sns_tokens_7_day_total_upper_bound_tokens`) is fully implemented and correct — it is simply never called: [7](#0-6) 

---

### Impact Explanation

Any SNS governance majority can pass an unlimited number of `MintSnsTokens` proposals within a 7-day window, minting an arbitrary quantity of SNS tokens. The 7-day rolling cap is the primary on-chain safeguard against runaway inflation via governance. With it disabled for minting, a governance majority can:

1. Dilute all existing token holders to near-zero in a single governance cycle.
2. Mint tokens to a controlled address to acquire overwhelming voting power, making the attack self-reinforcing.
3. Drain the SNS treasury indirectly by first minting tokens to inflate supply, then using `TransferSnsTreasuryFunds` (which still has its cap, but is now trivially bypassable via the inflated voting power).

The integration test explicitly acknowledges the bypass is live in production — the assertion that the second mint proposal should fail is commented out, and `unwrap()` is used instead, confirming the second proposal succeeds: [8](#0-7) 

---

### Likelihood Explanation

The attacker-controlled entry path is the standard `manage_neuron` → `make_proposal` → `MintSnsTokens` flow, callable by any SNS neuron holder with sufficient voting power. Many SNS DAOs have concentrated voting power (founding team, early investors, or a single whale neuron), making governance majority achievable. The bypass requires no special privileges, no key compromise, and no off-chain coordination beyond normal governance participation. The vulnerability is present in the currently deployed SNS governance canister code.

---

### Recommendation

Uncomment the real `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` (tracked as `TODO(NNS1-2982)`) and remove the `Decimal::MAX` stub. Additionally, add an execution-time re-check for `MintSnsTokens` analogous to `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` to guard against race conditions between proposal submission and execution. [9](#0-8) 

---

### Proof of Concept

The existing integration test in `rs/sns/integration_tests/src/sns_treasury.rs` already demonstrates the bypass. The test submits a second `MintSnsTokens` proposal that should be rejected by the rate limit, but the assertion is commented out and replaced with `unwrap()` (line 966), confirming the second proposal passes without restriction.

The call path for an unprivileged SNS neuron holder is:

1. Call `manage_neuron` on the SNS governance canister with `Command::MakeProposal(MintSnsTokens { amount_e8s: u64::MAX, ... })`.
2. Proposal passes governance vote.
3. `validate_and_render_mint_sns_tokens` is called → `treasury_valuation_if_proposal_amount_is_small_enough_or_err` is called → `MintSnsTokens::recent_amount_total_upper_bound_tokens` returns `Decimal::MAX` → cap check always passes.
4. Repeat immediately for a second proposal — no 7-day cooldown is enforced. [10](#0-9)

### Citations

**File:** rs/sns/governance/src/proposal.rs (L51-54)
```rust
use ic_sns_governance_proposals_amount_total_limit::{
    // TODO(NNS1-2982): Uncomment. mint_sns_tokens_7_day_total_upper_bound_tokens,
    transfer_sns_treasury_funds_7_day_total_upper_bound_tokens,
};
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

**File:** rs/sns/governance/src/proposal.rs (L875-930)
```rust
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

**File:** rs/sns/governance/src/proposal.rs (L1025-1041)
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

    // TODO(NNS1-2982): Delete.
    fn recent_amount_total_upper_bound_tokens(_valuation: &Valuation) -> Result<Decimal, String> {
        // Ideally, we'd return infinity, but Decimal does not have that. This is the next best
        // thing, and should be good enough, because we have already planned the obselences of this
        // code (see tickets NNS1-298(1|2)).
        Ok(Decimal::MAX)
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

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L14-18)
```rust
pub fn mint_sns_tokens_7_day_total_upper_bound_tokens(
    valuation: Valuation,
) -> Result<Decimal, ProposalsAmountTotalLimitError> {
    ProposalsAmountTotalUpperBound::in_tokens(valuation)
}
```

**File:** rs/sns/integration_tests/src/sns_treasury.rs (L942-966)
```rust
    /* TODO(NNS1-2982): Uncomment.
    let err = doomed_make_proposal_result.unwrap_err();
    let SnsGovernanceError {
        error_type,
        error_message,
    } = &err;
    assert_eq!(
        SnsErrorType::try_from(*error_type),
        Ok(SnsErrorType::InvalidProposal),
        "{:#?}",
        err,
    );
    let error_message = error_message.to_lowercase();
    for snip in [
        "amount",
        "too large",
        "2222",
        "upper bound",
        "exceeded",
        "try again",
    ] {
        assert!(error_message.contains(snip), "{:#?}", err);
    }
    */
    doomed_make_proposal_result.unwrap(); // TODO(NNS1-2982): Delete this line.
```
