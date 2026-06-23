### Title
`MintSnsTokens` Proposal Action Has No Enforced Upper-Bound on Mint Amount — (`rs/sns/governance/src/proposal.rs`)

### Summary

The SNS Governance canister's `MintSnsTokens` proposal action intentionally disables the 7-day rolling mint cap by returning `Decimal::MAX` from `recent_amount_total_upper_bound_tokens`, allowing any SNS governance majority to mint an unbounded quantity of SNS tokens to any recipient with no per-window limit enforced.

### Finding Description

The `TokenProposalAction` trait defines `recent_amount_total_upper_bound_tokens` to cap the total tokens that can be minted via `MintSnsTokens` proposals within a 7-day window. The implementation for `TransferSnsTreasuryFunds` correctly calls `transfer_sns_treasury_funds_7_day_total_upper_bound_tokens`, which enforces a treasury-fraction-based cap.

However, the `MintSnsTokens` implementation of this method is deliberately stubbed out to return `Decimal::MAX`:

```rust
// TODO(NNS1-2982): Delete.
fn recent_amount_total_upper_bound_tokens(_valuation: &Valuation) -> Result<Decimal, String> {
    Ok(Decimal::MAX)
}
```

The correct implementation — which calls `mint_sns_tokens_7_day_total_upper_bound_tokens` — is commented out:

```rust
/* TODO(NNS1-2982): Uncomment.
fn recent_amount_total_upper_bound_tokens(valuation: &Valuation) -> Result<Decimal, String> {
    mint_sns_tokens_7_day_total_upper_bound_tokens(*valuation)
        ...
}
*/
```

The enforcement path in `treasury_valuation_if_proposal_amount_is_small_enough_or_err` compares `proposal_amount_tokens > allowance_remainder_tokens`. Since `allowance_remainder_tokens = Decimal::MAX - spent_tokens ≈ Decimal::MAX`, this check never triggers for any realistic mint amount.

The integration test in `rs/sns/integration_tests/src/sns_treasury.rs` confirms this: the second `MintSnsTokens` proposal that should be rejected by the limit is instead allowed to succeed, with the rejection assertion commented out and replaced with `doomed_make_proposal_result.unwrap()`.

### Impact Explanation

Any SNS that has the `MintSnsTokens` proposal type available (all SNSes) can pass a governance proposal to mint an arbitrary number of SNS tokens — up to `u64::MAX` e8s per proposal — to any recipient, with no 7-day rolling cap enforced. A governance majority (which may be a small quorum depending on neuron distribution) can inflate the SNS token supply without bound, diluting all existing token holders and potentially draining value from the SNS treasury or ecosystem. This is a direct ledger conservation violation: the minting account can credit tokens to any account without any economic constraint beyond the governance vote threshold.

### Likelihood Explanation

The vulnerability is reachable by any SNS governance majority. On SNSes with concentrated neuron ownership or low participation, a single large neuron holder can pass `MintSnsTokens` proposals unilaterally. The code path is fully deployed and active — the cap is not a future feature but a deliberately disabled guard with a `TODO` ticket. Any SNS token holder or external observer can verify the absence of the cap by inspecting the on-chain governance canister or the open-source code.

### Recommendation

Uncomment the correct `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` and delete the `Decimal::MAX` stub, as indicated by the `TODO(NNS1-2982)` comments. The correct implementation already exists and is ready to be activated:

```rust
fn recent_amount_total_upper_bound_tokens(valuation: &Valuation) -> Result<Decimal, String> {
    mint_sns_tokens_7_day_total_upper_bound_tokens(*valuation)
        .map_err(|treasury_limit_error| {
            format!("Unable to validate amount: {:?}", treasury_limit_error)
        })
}
```

Also uncomment the corresponding rejection assertion in the integration test `sns_can_mint_funds_via_proposals` and remove the `doomed_make_proposal_result.unwrap()` line.

### Proof of Concept

**Root cause — disabled cap:** [1](#0-0) 

**Correct implementation that is commented out:** [2](#0-1) 

**Enforcement check that is bypassed (always passes when max = Decimal::MAX):** [3](#0-2) 

**Integration test confirming the second unlimited mint succeeds instead of being rejected:** [4](#0-3) 

**The `perform_mint_sns_tokens` execution path that mints with no additional cap check:** [5](#0-4) 

**The cap library that is implemented but not called for `MintSnsTokens`:** [6](#0-5)

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

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L14-18)
```rust
pub fn mint_sns_tokens_7_day_total_upper_bound_tokens(
    valuation: Valuation,
) -> Result<Decimal, ProposalsAmountTotalLimitError> {
    ProposalsAmountTotalUpperBound::in_tokens(valuation)
}
```
