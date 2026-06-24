### Title
`MintSnsTokens` 7-Day Supply Cap Not Enforced — (`rs/sns/governance/src/proposal.rs`)

### Summary

The SNS governance canister intentionally implements a 7-day rolling cap on `MintSnsTokens` proposals (analogous to the `TransferSnsTreasuryFunds` cap), but the enforcement function `recent_amount_total_upper_bound_tokens` for `MintSnsTokens` is deliberately stubbed out to return `Decimal::MAX` (effectively infinity), meaning any amount of SNS tokens can be minted via governance proposals with no rate limit enforced.

### Finding Description

The `TokenProposalAction` trait defines `recent_amount_total_upper_bound_tokens` as the mechanism that caps how many tokens can be minted or transferred within a 7-day window. For `TransferSnsTreasuryFunds`, this is properly wired to `transfer_sns_treasury_funds_7_day_total_upper_bound_tokens`, which computes a treasury-size-proportional limit.

For `MintSnsTokens`, however, the real implementation is commented out and replaced with a stub that unconditionally returns `Decimal::MAX`:

```rust
// TODO(NNS1-2982): Delete.
fn recent_amount_total_upper_bound_tokens(_valuation: &Valuation) -> Result<Decimal, String> {
    // Ideally, we'd return infinity, but Decimal does not have that. This is the next best
    // thing, and should be good enough, because we have already planned the obselences of this
    // code (see tickets NNS1-298(1|2)).
    Ok(Decimal::MAX)
}
```

The correct implementation is commented out:

```rust
/* TODO(NNS1-2982): Uncomment.
fn recent_amount_total_upper_bound_tokens(valuation: &Valuation) -> Result<Decimal, String> {
    mint_sns_tokens_7_day_total_upper_bound_tokens(*valuation)
        ...
}
*/
```

The import of `mint_sns_tokens_7_day_total_upper_bound_tokens` is also commented out at the top of the file. The integration test `sns_can_mint_funds_via_proposals` confirms this: the second mint proposal that should be rejected is instead accepted (`doomed_make_proposal_result.unwrap()`), and the whale's balance doubles to `4_444` tokens instead of staying at `2_222`.

The enforcement path in `treasury_valuation_if_proposal_amount_is_small_enough_or_err` calls `recent_amount_total_upper_bound_tokens` to get `max_tokens`, then checks `proposal_amount_tokens > allowance_remainder_tokens`. With `max_tokens = Decimal::MAX`, the check `proposal_amount_tokens > Decimal::MAX - spent_tokens` is always false, so no proposal is ever rejected for exceeding the mint cap.

### Impact Explanation

Any SNS governance majority (which is the required role here — a whale neuron or coordinated voters) can pass unlimited `MintSnsTokens` proposals within any 7-day window, bypassing the intended treasury-proportional cap. This allows unbounded SNS token inflation via governance proposals, diluting all token holders. The `TransferSnsTreasuryFunds` cap is enforced; the `MintSnsTokens` cap is not, creating an asymmetry where minting is unconstrained while treasury transfers are limited.

### Likelihood Explanation

The vulnerability is reachable by any SNS governance majority. An SNS with a concentrated token holder (whale) or a coordinated group can pass repeated `MintSnsTokens` proposals in rapid succession. The integration test explicitly demonstrates this is currently possible on mainnet SNS deployments. The code comment acknowledges this is a known temporary state pending ticket NNS1-2982.

### Recommendation

Uncomment the real `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` and the corresponding import of `mint_sns_tokens_7_day_total_upper_bound_tokens`. Remove the `Decimal::MAX` stub. Also uncomment the enforcement assertion in the integration test `sns_can_mint_funds_via_proposals`.

### Proof of Concept

1. The stub returning `Decimal::MAX` is live in production: [1](#0-0) 

2. The real cap function exists but is commented out, along with its import: [2](#0-1) [3](#0-2) 

3. The enforcement check uses the return value of `recent_amount_total_upper_bound_tokens` as `max_tokens`; with `Decimal::MAX`, the check never triggers: [4](#0-3) 

4. The integration test confirms a second mint proposal that should be rejected is currently accepted, and the whale's balance doubles: [5](#0-4) 

5. By contrast, `TransferSnsTreasuryFunds` correctly enforces its cap: [6](#0-5)

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
