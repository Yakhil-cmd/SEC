### Title
`MintSnsTokens` 7-Day Minting Limit Disabled — Unlimited SNS Token Minting via Governance Proposals - (File: `rs/sns/governance/src/proposal.rs`)

### Summary
The `MintSnsTokens` governance proposal action in SNS governance has its 7-day minting limit enforcement intentionally disabled. The `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` returns `Decimal::MAX` instead of the proper treasury-valuation-based cap, meaning any amount of SNS tokens can be minted through successive governance proposals within a 7-day window without hitting any limit. This is a direct analog to the reported wrong-constant mint-limit bug: instead of a value 10× too large, the limit is set to the maximum representable value, making it effectively infinite.

### Finding Description
In `rs/sns/governance/src/proposal.rs`, the `TokenProposalAction` trait implementation for `MintSnsTokens` has its `recent_amount_total_upper_bound_tokens` method returning `Decimal::MAX`:

```rust
// TODO(NNS1-2982): Delete.
fn recent_amount_total_upper_bound_tokens(_valuation: &Valuation) -> Result<Decimal, String> {
    Ok(Decimal::MAX)
}
``` [1](#0-0) 

The correct implementation — which calls `mint_sns_tokens_7_day_total_upper_bound_tokens` to compute a treasury-valuation-based cap — is commented out:

```rust
/* TODO(NNS1-2982): Uncomment.
fn recent_amount_total_upper_bound_tokens(valuation: &Valuation) -> Result<Decimal, String> {
    mint_sns_tokens_7_day_total_upper_bound_tokens(*valuation)
        ...
}
*/
``` [2](#0-1) 

The import of `mint_sns_tokens_7_day_total_upper_bound_tokens` is also commented out: [3](#0-2) 

The enforcement check at line 805 computes `allowance_remainder_tokens = Decimal::MAX - spent_tokens`, which is always astronomically large, so the guard `if proposal_amount_tokens > allowance_remainder_tokens` never fires for any realistic proposal amount. [4](#0-3) 

The correct limit logic exists and is fully implemented in `rs/sns/governance/proposals_amount_total_limit/src/lib.rs` — it caps minting at 25% of treasury per 7 days for medium-sized treasuries, and at 300,000 XDR for large ones — but it is never called for `MintSnsTokens`: [5](#0-4) 

### Impact Explanation
Any SNS governance majority can pass multiple `MintSnsTokens` proposals within a 7-day window, minting an unlimited quantity of SNS tokens. The intended treasury-based minting limit — designed to prevent excessive dilution — is completely bypassed. This allows unbounded inflation of the SNS token supply, directly harming all existing token holders through dilution and potentially collapsing the token's value.

### Likelihood Explanation
Medium. Exploitation requires a governance majority (a whale neuron or coalition of neuron holders) to pass successive `MintSnsTokens` proposals. In SNS DAOs with concentrated voting power this is realistic. Crucially, the bug is a wrong constant (Decimal::MAX instead of the treasury-based cap), not a governance attack — even a well-intentioned majority has no code-enforced guardrail preventing over-minting.

### Recommendation
Uncomment the correct `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` and the `mint_sns_tokens_7_day_total_upper_bound_tokens` import, then delete the temporary `Decimal::MAX` stub. The correct implementation is already written and tested in `rs/sns/governance/proposals_amount_total_limit/src/lib.rs`.

### Proof of Concept
The integration test at `rs/sns/integration_tests/src/sns_treasury.rs` explicitly demonstrates the bug: a second `MintSnsTokens` proposal that **should** be rejected due to minting limits is expected to **succeed**:

```rust
/* TODO(NNS1-2982): Uncomment.
let err = doomed_make_proposal_result.unwrap_err();
...
assert_eq!(SnsErrorType::try_from(*error_type), Ok(SnsErrorType::InvalidProposal), ...);
*/
doomed_make_proposal_result.unwrap(); // TODO(NNS1-2982): Delete this line.
``` [6](#0-5) 

Step-by-step:
1. SNS governance majority submits `MintSnsTokens` for 2,222 SNS tokens → proposal passes and executes.
2. Same majority immediately submits another `MintSnsTokens` for 2,222 SNS tokens → proposal passes and executes (limit not enforced).
3. Steps can be repeated indefinitely within the 7-day window, minting arbitrary amounts of SNS tokens.

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
