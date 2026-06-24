Audit Report

## Title
Missing 7-Day Rolling Supply Cap on `MintSnsTokens` Proposals Enables Unbounded SNS Token Inflation — (File: `rs/sns/governance/src/proposal.rs`)

## Summary
The `MintSnsTokens` SNS governance proposal action has its 7-day rolling minting cap intentionally disabled in production code. The real enforcement function is commented out and replaced with a stub returning `Decimal::MAX`, meaning any SNS governance majority can pass unlimited back-to-back minting proposals with no protocol-level rate limit, inflating the SNS token supply without bound. The integration test explicitly confirms both proposals succeed and the resulting balance is `2 * 2_222 = 4_444` tokens.

## Finding Description
The SNS governance system defines a shared validation framework (`treasury_valuation_if_proposal_amount_is_small_enough_or_err`) that enforces a 7-day rolling cap on token-moving proposals. For `TransferSnsTreasuryFunds`, the cap is fully enforced via `transfer_sns_treasury_funds_7_day_total_upper_bound_tokens`.

For `MintSnsTokens`, the analogous implementation is commented out: [1](#0-0) 

The stub unconditionally returns `Decimal::MAX`, making the check at proposal submission time (`proposal_amount_tokens > allowance_remainder_tokens`) always false: [2](#0-1) 

The real limit function `mint_sns_tokens_7_day_total_upper_bound_tokens` is fully implemented in the limit library: [3](#0-2) 

It is imported but commented out: [4](#0-3) 

Additionally, `TransferSnsTreasuryFunds` has a second enforcement check at execution time (`transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err`): [5](#0-4) 

No analogous execution-time check exists for `MintSnsTokens`, leaving no fallback guard.

The integration test confirms the bypass is live — the assertion that the second large mint should be rejected is commented out, `doomed_make_proposal_result.unwrap()` confirms the second mint succeeds, and the expected balance is `2 * 2_222 = 4_444`: [6](#0-5) 

## Impact Explanation
This is a significant SNS security impact with concrete user harm. Any SNS governance majority can submit unlimited `MintSnsTokens` proposals in rapid succession within a 7-day window, minting new SNS tokens from thin air. This directly dilutes the voting power and economic stake of all existing token holders, undermines token scarcity, and depresses token price. This matches the allowed High impact: "Significant SNS security impact with concrete user or protocol harm." The Critical "illegal minting" category requires exorbitant ICP/Cycles or chain-key assets (especially over $1M); SNS-specific tokens do not meet that threshold, placing this at **High ($2,000–$10,000)**.

## Likelihood Explanation
The SNS governance model is permissionless — any party can acquire SNS tokens and neurons on the open market. No special protocol privilege or insider access is required beyond accumulating or coordinating sufficient voting power to pass proposals, which is the intended governance mechanism. The bypass is not a configuration gap but a code-level stub (`Decimal::MAX`) active on every deployed SNS canister using this governance code. The `TODO(NNS1-2982)` markers confirm this is a known incomplete implementation shipped to production.

## Recommendation
1. Uncomment the real `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` (remove the `/* TODO(NNS1-2982): Uncomment. */` block and delete the `Decimal::MAX` stub) at `rs/sns/governance/src/proposal.rs` L1025–1041.
2. Uncomment the import of `mint_sns_tokens_7_day_total_upper_bound_tokens` at L51–54.
3. Add an execution-time check for `MintSnsTokens` analogous to `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err`.
4. Uncomment the integration test assertion in `rs/sns/integration_tests/src/sns_treasury.rs` L942–965 and delete the `doomed_make_proposal_result.unwrap()` stub at L966 and the `2 *` multiplier at L978.

## Proof of Concept
1. Deploy an SNS with a treasury valued above 100,000 XDR (medium or large regime).
2. Acquire or coordinate enough neuron voting power to pass proposals.
3. Submit `MintSnsTokens` proposal #1 for an amount equal to 25% of the treasury token supply. It passes and executes.
4. Immediately submit `MintSnsTokens` proposal #2 for the same amount. Because `recent_amount_total_upper_bound_tokens` returns `Decimal::MAX`, the check `proposal_amount_tokens > allowance_remainder_tokens` is always false, and the proposal is accepted and executed.
5. Repeat indefinitely within the same 7-day window. The integration test at `rs/sns/integration_tests/src/sns_treasury.rs` L966 and L977–983 directly confirms this behavior, with the resulting balance being `2 * 2_222 = 4_444` tokens.

### Citations

**File:** rs/sns/governance/src/proposal.rs (L51-54)
```rust
use ic_sns_governance_proposals_amount_total_limit::{
    // TODO(NNS1-2982): Uncomment. mint_sns_tokens_7_day_total_upper_bound_tokens,
    transfer_sns_treasury_funds_7_day_total_upper_bound_tokens,
};
```

**File:** rs/sns/governance/src/proposal.rs (L805-814)
```rust
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

**File:** rs/sns/governance/src/proposal.rs (L2600-2617)
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
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L14-18)
```rust
pub fn mint_sns_tokens_7_day_total_upper_bound_tokens(
    valuation: Valuation,
) -> Result<Decimal, ProposalsAmountTotalLimitError> {
    ProposalsAmountTotalUpperBound::in_tokens(valuation)
}
```

**File:** rs/sns/integration_tests/src/sns_treasury.rs (L942-983)
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

    // Whale's balance is not affected by the second proposal.
    let balance = icrc1_balance(
        &state_machine,
        sns_ledger_canister_id,
        Account {
            owner: Principal::from(*WHALE),
            subaccount: None,
        },
    );
    let expected_balance_tokens = Tokens::new(
        2 * // TODO(NNS1-2982): Delete this line.
        2_222,
        0,
    )
    .unwrap();
    assert_eq!(balance, expected_balance_tokens);
```
