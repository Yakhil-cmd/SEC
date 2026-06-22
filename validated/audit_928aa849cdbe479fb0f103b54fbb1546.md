### Title
SNS `MintSnsTokens` Proposal Rate Limit Intentionally Disabled — Governance Majority Can Mint Unlimited Tokens and Dilute Existing Holders - (File: rs/sns/governance/src/proposal.rs)

### Summary

The SNS governance system includes a `MintSnsTokens` proposal action that is supposed to be rate-limited (capped at a fraction of treasury value per 7-day window, identical to `TransferSnsTreasuryFunds`). However, the rate-limiting upper-bound function is intentionally commented out in production code and replaced with `Decimal::MAX`, removing all minting caps. Any SNS governance majority can pass proposals to mint an unlimited number of SNS tokens to any address, diluting existing token holders' proportional ownership and voting power with no on-chain protection.

### Finding Description

The `TokenProposalAction` trait defines `recent_amount_total_upper_bound_tokens` to cap how many tokens can be minted or transferred within a 7-day window. For `TransferSnsTreasuryFunds`, this cap is properly enforced via `transfer_sns_treasury_funds_7_day_total_upper_bound_tokens`. For `MintSnsTokens`, the analogous cap function `mint_sns_tokens_7_day_total_upper_bound_tokens` exists and is fully implemented in `rs/sns/governance/proposals_amount_total_limit/src/lib.rs`, but its call site is commented out in the `MintSnsTokens` implementation:

```rust
// TODO(NNS1-2982): Uncomment.
/* fn recent_amount_total_upper_bound_tokens(valuation: &Valuation) -> Result<Decimal, String> {
    mint_sns_tokens_7_day_total_upper_bound_tokens(*valuation)
    ...
} */

// TODO(NNS1-2982): Delete.
fn recent_amount_total_upper_bound_tokens(_valuation: &Valuation) -> Result<Decimal, String> {
    Ok(Decimal::MAX)  // effectively no limit
}
```

The validation function `treasury_valuation_if_proposal_amount_is_small_enough_or_err` calls `recent_amount_total_upper_bound_tokens` to enforce the cap. Because `MintSnsTokens` returns `Decimal::MAX`, the check at line 805 (`if proposal_amount_tokens > allowance_remainder_tokens`) never triggers, and any amount can be minted in a single proposal or across multiple proposals within the same 7-day window.

The integration test that was supposed to verify the rate limit is also commented out, and the line asserting that a second large mint **succeeds** (when it should fail) is the active production code path:

```rust
/* TODO(NNS1-2982): Uncomment.
let err = doomed_make_proposal_result.unwrap_err();
...
*/
doomed_make_proposal_result.unwrap(); // TODO(NNS1-2982): Delete this line.
```

### Impact Explanation

Any SNS governance majority (including a single whale neuron holder controlling >50% of voting power, or a coordinated group) can:

1. Submit a `MintSnsTokens` proposal for an arbitrarily large amount.
2. Vote it through with their majority.
3. `perform_mint_sns_tokens` executes `self.ledger.transfer_funds(amount_e8s, 0, None, to, mint.memo())` — a minting transfer (no `from` account) with no cap.

This inflates the total SNS token supply without bound, diluting every existing token holder's proportional ownership and every neuron holder's voting power. Unlike `TransferSnsTreasuryFunds` (which is capped at 25% of treasury value per 7 days for medium treasuries, or 300,000 XDR for large ones), `MintSnsTokens` has no effective ceiling. Minority token holders have no on-chain protection against this dilution.

### Likelihood Explanation

The entry path is the standard SNS governance proposal mechanism, reachable by any `ledger/governance user` (neuron holder). No special access, leaked keys, or subnet compromise is required — only a governance majority, which is the normal operating condition for SNS governance. The disabled rate limit is a production-deployed state confirmed by the `TODO(NNS1-2982)` markers and the commented-out integration test assertion.

### Recommendation

Uncomment the correct `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` (which calls `mint_sns_tokens_7_day_total_upper_bound_tokens`) and delete the temporary `Decimal::MAX` placeholder. Also uncomment the corresponding integration test assertion in `rs/sns/integration_tests/src/sns_treasury.rs` to prevent regression.

### Proof of Concept

**Root cause — rate limit returns `Decimal::MAX`:** [1](#0-0) 

**The real cap function exists but is commented out at the import site:** [2](#0-1) 

**The validation logic that enforces the cap (correctly used for `TransferSnsTreasuryFunds`, bypassed for `MintSnsTokens`):** [3](#0-2) 

**`perform_mint_sns_tokens` executes with no amount cap:** [4](#0-3) 

**Integration test confirms the second unlimited mint succeeds (rate limit not enforced):** [5](#0-4) 

**The cap function is fully implemented but unused:** [6](#0-5)

### Citations

**File:** rs/sns/governance/src/proposal.rs (L51-54)
```rust
use ic_sns_governance_proposals_amount_total_limit::{
    // TODO(NNS1-2982): Uncomment. mint_sns_tokens_7_day_total_upper_bound_tokens,
    transfer_sns_treasury_funds_7_day_total_upper_bound_tokens,
};
```

**File:** rs/sns/governance/src/proposal.rs (L793-814)
```rust
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

**File:** rs/sns/governance/src/governance.rs (L3084-3087)
```rust
        self.ledger
            .transfer_funds(amount_e8s, 0, None, to, mint.memo())
            .await?;
        Ok(())
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

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L14-18)
```rust
pub fn mint_sns_tokens_7_day_total_upper_bound_tokens(
    valuation: Valuation,
) -> Result<Decimal, ProposalsAmountTotalLimitError> {
    ProposalsAmountTotalUpperBound::in_tokens(valuation)
}
```
