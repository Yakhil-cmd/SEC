### Title
`MintSnsTokens` 7-Day Minting Rate Limit Permanently Disabled — (`rs/sns/governance/src/proposal.rs`)

### Summary

The `MintSnsTokens` SNS governance proposal action has its treasury-based 7-day minting upper bound intentionally stubbed out to return `Decimal::MAX` (no limit). The correct enforcement function is commented out pending ticket NNS1-2982. As a result, any SNS governance majority can pass back-to-back `MintSnsTokens` proposals to mint an unlimited quantity of SNS tokens, with no rate-limit enforcement whatsoever.

### Finding Description

The `TokenProposalAction` trait defines `recent_amount_total_upper_bound_tokens` to cap the total tokens that may be minted within a rolling 7-day window, based on the current treasury valuation.

For `TransferSnsTreasuryFunds`, this is correctly wired to `transfer_sns_treasury_funds_7_day_total_upper_bound_tokens`: [1](#0-0) 

For `MintSnsTokens`, the correct implementation is **commented out**: [2](#0-1) 

The live stub unconditionally returns `Decimal::MAX`. The enforcement check in `treasury_valuation_if_proposal_amount_is_small_enough_or_err` computes:

```
allowance_remainder_tokens = Decimal::MAX - spent_tokens  ≈ Decimal::MAX
```

So the guard `if proposal_amount_tokens > allowance_remainder_tokens` at line 805 **never fires**: [3](#0-2) 

The integration test `sns_can_mint_funds_via_proposals` explicitly confirms this: the assertion that a second mint proposal should be **rejected** is commented out, and `doomed_make_proposal_result.unwrap()` is called instead — the second mint succeeds: [4](#0-3) 

### Impact Explanation

A governance majority (including a single whale neuron holder with >50% voting power) can submit an unbounded sequence of `MintSnsTokens` proposals, each minting up to `u64::MAX` e8s of SNS tokens. The intended safety guardrail — limiting minting to at most 25% of treasury value per 7-day window — is completely absent. This allows unlimited inflation of the SNS token supply, diluting all other token holders and potentially draining the SNS treasury's economic value.

### Likelihood Explanation

Medium. Every deployed SNS is affected. Any governance participant who can achieve a voting majority (or who already holds a dominant neuron) can exploit this without any privileged key or external dependency. The `MintSnsTokens` proposal type is a standard, publicly documented SNS action reachable via normal ingress to the SNS governance canister.

### Recommendation

Uncomment the correct `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` (the block marked `TODO(NNS1-2982): Uncomment`) and delete the stub that returns `Decimal::MAX`: [2](#0-1) 

Simultaneously, uncomment the corresponding assertion in the integration test: [4](#0-3) 

### Proof of Concept

1. Deploy an SNS with a whale neuron holding >50% voting power.
2. Submit `MintSnsTokens { amount_e8s: Some(u64::MAX), to_principal: Some(whale), ... }` as a proposal.
3. Vote to adopt. The proposal passes `validate_and_render_mint_sns_tokens` because `recent_amount_total_upper_bound_tokens` returns `Decimal::MAX`, so `proposal_amount_tokens > allowance_remainder_tokens` is always `false`.
4. `perform_mint_sns_tokens` executes, calling `self.ledger.transfer_funds(u64::MAX, ...)` from the SNS minting account.
5. Repeat indefinitely — no 7-day window check ever blocks a subsequent proposal. [5](#0-4)

### Citations

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
