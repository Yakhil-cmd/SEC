The code evidence is conclusive. Let me verify the integration test context to complete the analysis.

### Title
MintSnsTokens 7-Day Rate Limit Completely Bypassed via `Decimal::MAX` Stub — (`rs/sns/governance/src/proposal.rs`)

### Summary

The `MintSnsTokens` implementation of `recent_amount_total_upper_bound_tokens` unconditionally returns `Decimal::MAX` instead of a real treasury-based limit. The real enforcement function exists and is wired up for `TransferSnsTreasuryFunds`, but is commented out for `MintSnsTokens` behind a `TODO(NNS1-2982)`. As a result, the 7-day minting rate-limit guard is entirely inoperative in production, and any SNS governance majority can mint an unbounded quantity of SNS tokens within any 7-day window.

### Finding Description

The `TokenProposalAction` trait defines `recent_amount_total_upper_bound_tokens` as the ceiling that the rolling 7-day minted total must not exceed. [1](#0-0) 

For `TransferSnsTreasuryFunds` the real limit is enforced: [2](#0-1) 

For `MintSnsTokens`, the real implementation is commented out and replaced with a stub that returns `Decimal::MAX`: [3](#0-2) 

The enforcement check in `treasury_valuation_if_proposal_amount_is_small_enough_or_err` computes `allowance_remainder_tokens = Decimal::MAX - spent_tokens`, which is always astronomically large, so the guard: [4](#0-3) 

never fires for `MintSnsTokens`. The real bounding function `mint_sns_tokens_7_day_total_upper_bound_tokens` exists and is correct: [5](#0-4) 

but its import is also commented out at the top of `proposal.rs`: [6](#0-5) 

The integration test explicitly confirms the bypass: the comment says "Whale tries again, but this time, it doesn't work, because of minting limits," yet the assertion is `unwrap()` (success) rather than `unwrap_err()`: [7](#0-6) 

### Impact Explanation

Any SNS governance majority (a whale neuron holder or a colluding group) can submit an unlimited number of `MintSnsTokens` proposals within a 7-day window, each minting up to `u64::MAX` e8s of SNS tokens. The rate-limiting mechanism is the only protocol-level guardrail preventing runaway inflation of SNS token supply. With it disabled, the total supply can be inflated arbitrarily fast, diluting all existing token holders and collapsing the token's value.

### Likelihood Explanation

The exploit path requires only standard SNS governance participation: a neuron with `SubmitProposal` permission and enough voting power to pass proposals. No privileged keys, no subnet compromise, and no external oracle manipulation are needed. The bypass is unconditional — it applies to every SNS instance running this code, regardless of treasury size or token price. The integration test already demonstrates the exact attack sequence passing end-to-end.

### Recommendation

Remove the `Decimal::MAX` stub and uncomment the real implementation as the TODO instructs:

1. In `rs/sns/governance/src/proposal.rs`, uncomment the `mint_sns_tokens_7_day_total_upper_bound_tokens` import (line 52) and the real `recent_amount_total_upper_bound_tokens` body (lines 1025–1033), then delete the stub (lines 1035–1041).
2. In `rs/sns/integration_tests/src/sns_treasury.rs`, uncomment the `unwrap_err()` assertion block (lines 942–965) and delete the `unwrap()` line (line 966).

### Proof of Concept

```
// State-machine test sketch (mirrors existing integration test structure)
// 1. Create SNS with a whale neuron holding majority voting power.
// 2. Submit Proposal A: MintSnsTokens { amount_e8s: u64::MAX }  → passes validation, executes.
// 3. Immediately submit Proposal B: MintSnsTokens { amount_e8s: u64::MAX }  → also passes validation.
// 4. Repeat N times within 7 days.
// 5. Assert: all proposals executed; total minted = N * u64::MAX e8s.
//    (The existing integration test at sns_treasury.rs:930-966 already demonstrates step 2-3.)
```

The existing integration test at `rs/sns/integration_tests/src/sns_treasury.rs` line 966 already proves this: `doomed_make_proposal_result.unwrap()` succeeds where `unwrap_err()` is the intended behavior.

### Citations

**File:** rs/sns/governance/src/proposal.rs (L51-54)
```rust
use ic_sns_governance_proposals_amount_total_limit::{
    // TODO(NNS1-2982): Uncomment. mint_sns_tokens_7_day_total_upper_bound_tokens,
    transfer_sns_treasury_funds_7_day_total_upper_bound_tokens,
};
```

**File:** rs/sns/governance/src/proposal.rs (L753-755)
```rust
    /// The greatest that recent_amount_total_tokens is allowed to be. This is based on the value of
    /// the token is in the treasury.
    fn recent_amount_total_upper_bound_tokens(valuation: &Valuation) -> Result<Decimal, String>;
```

**File:** rs/sns/governance/src/proposal.rs (L801-814)
```rust
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

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L14-18)
```rust
pub fn mint_sns_tokens_7_day_total_upper_bound_tokens(
    valuation: Valuation,
) -> Result<Decimal, ProposalsAmountTotalLimitError> {
    ProposalsAmountTotalUpperBound::in_tokens(valuation)
}
```

**File:** rs/sns/integration_tests/src/sns_treasury.rs (L930-966)
```rust
    // Whale tries again, but this time, it doesn't work, because of minting limits.
    let doomed_make_proposal_result = sns_make_proposal(
        &state_machine,
        governance_canister_id,
        *WHALE,
        whale_neuron_id,
        Proposal {
            title: "Second Mint".to_string(),
            ..proposal
        },
    );

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
