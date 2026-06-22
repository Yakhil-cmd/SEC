### Title
`MintSnsTokens` 7-Day Minting Cap Not Enforced — (`File: rs/sns/governance/src/proposal.rs`)

### Summary
The `MintSnsTokens` SNS governance proposal action has its 7-day minting upper-bound check deliberately disabled in production code. The `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` unconditionally returns `Decimal::MAX` instead of the treasury-valuation-based cap, meaning any amount of SNS tokens can be minted via governance proposals with no rate limit enforced.

### Finding Description
The `TokenProposalAction` trait defines `recent_amount_total_upper_bound_tokens` to return the maximum tokens that may be minted within a 7-day rolling window, based on treasury valuation. For `TransferSnsTreasuryFunds`, this limit is correctly wired to `transfer_sns_treasury_funds_7_day_total_upper_bound_tokens`. For `MintSnsTokens`, the analogous call to `mint_sns_tokens_7_day_total_upper_bound_tokens` is commented out and replaced with a stub that returns `Decimal::MAX`: [1](#0-0) 

The import of `mint_sns_tokens_7_day_total_upper_bound_tokens` is also commented out at the module level: [2](#0-1) 

The enforcement function `treasury_valuation_if_proposal_amount_is_small_enough_or_err` computes `allowance_remainder_tokens = max_tokens - spent_tokens`. When `max_tokens = Decimal::MAX`, the check `proposal_amount_tokens > allowance_remainder_tokens` is always `false`, so no proposal is ever rejected for exceeding the minting cap: [3](#0-2) 

The `perform_mint_sns_tokens` execution path performs no additional cap check and directly calls the ledger to mint: [4](#0-3) 

An integration test explicitly confirms the bypass: a second `MintSnsTokens` proposal that should be rejected by the minting limit is instead expected to succeed, with the correct rejection assertion commented out under `TODO(NNS1-2982)`: [5](#0-4) 

### Impact Explanation
Any SNS governance participant who can pass a `MintSnsTokens` proposal can mint an unbounded quantity of SNS tokens within any 7-day window, bypassing the treasury-valuation-based cap that is intended to limit inflation to at most 25–100% of treasury value per week. This directly violates the ledger conservation invariant for SNS tokens: the total supply can be inflated arbitrarily via repeated proposals, diluting all existing token holders. The `mint_sns_tokens_7_day_total_upper_bound_tokens` function and the `ProposalsAmountTotalUpperBound` logic exist and are correct — they are simply not connected to the enforcement path for `MintSnsTokens`. [6](#0-5) 

### Likelihood Explanation
Any SNS with a whale neuron holder or a coalition of voters sufficient to pass proposals can exploit this. The bypass requires no special access beyond normal SNS governance participation. The code comment explicitly acknowledges this is a known incomplete state (`TODO(NNS1-2982): Delete this line`), confirming the limit is intentionally absent in the current production deployment. Every deployed SNS is affected.

### Recommendation
Uncomment the correct `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` (lines 1025–1033 of `rs/sns/governance/src/proposal.rs`) and delete the `Decimal::MAX` stub (lines 1035–1041). Also uncomment the `mint_sns_tokens_7_day_total_upper_bound_tokens` import. Uncomment the rejection assertion in the integration test at `rs/sns/integration_tests/src/sns_treasury.rs:942–965` and delete line 966.

### Proof of Concept
1. Deploy any SNS with a treasury holding SNS tokens.
2. Submit a `MintSnsTokens` proposal for an amount exceeding 25% of the treasury value (e.g., 100% of treasury).
3. Pass the proposal through governance voting.
4. Observe that `validate_and_render_mint_sns_tokens` calls `treasury_valuation_if_proposal_amount_is_small_enough_or_err`, which calls `MintSnsTokens::recent_amount_total_upper_bound_tokens`, which returns `Decimal::MAX`.
5. The check `proposal_amount_tokens > Decimal::MAX - spent_tokens` is always `false`; the proposal is accepted and executed.
6. Repeat immediately with another proposal of the same size — again accepted, with no 7-day window enforcement.
7. The SNS ledger total supply grows without bound, confirmed by the integration test at line 977–983 which shows `2 * 2_222` tokens minted (double the intended cap) as the expected "correct" behavior under the current broken state. [7](#0-6)

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

**File:** rs/sns/governance/src/governance.rs (L3062-3088)
```rust
    async fn perform_mint_sns_tokens(
        &mut self,
        mint: MintSnsTokens,
    ) -> Result<(), GovernanceError> {
        let to = Account {
            owner: mint
                .to_principal
                .ok_or(GovernanceError::new_with_message(
                    ErrorType::InvalidProposal,
                    "Expected mint to have a target principal",
                ))?
                .0,
            subaccount: mint
                .to_subaccount
                .as_ref()
                .map(|s| bytes_to_subaccount(&s.subaccount[..]))
                .transpose()?,
        };
        let amount_e8s = mint.amount_e8s.ok_or(GovernanceError::new_with_message(
            ErrorType::InvalidProposal,
            "Expected MintSnsTokens to have an an amount_e8s",
        ))?;
        self.ledger
            .transfer_funds(amount_e8s, 0, None, to, mint.memo())
            .await?;
        Ok(())
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

**File:** rs/sns/integration_tests/src/sns_treasury.rs (L977-983)
```rust
    let expected_balance_tokens = Tokens::new(
        2 * // TODO(NNS1-2982): Delete this line.
        2_222,
        0,
    )
    .unwrap();
    assert_eq!(balance, expected_balance_tokens);
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L14-18)
```rust
pub fn mint_sns_tokens_7_day_total_upper_bound_tokens(
    valuation: Valuation,
) -> Result<Decimal, ProposalsAmountTotalLimitError> {
    ProposalsAmountTotalUpperBound::in_tokens(valuation)
}
```
