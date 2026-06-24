### Title
Missing Upper Bound on `MintSnsTokens` 7-Day Rate Limit Allows Unlimited SNS Token Inflation - (`File: rs/sns/governance/src/proposal.rs`)

### Summary

The `MintSnsTokens` proposal action in SNS governance has its 7-day minting rate limit intentionally disabled via a `TODO(NNS1-2982)` stub that returns `Decimal::MAX` as the upper bound. The check that should reject proposals exceeding the treasury-proportional cap is commented out, allowing an SNS neuron holder with sufficient voting power to mint an unlimited number of SNS tokens within any 7-day window, bypassing the rate-limiting mechanism that exists and is enforced for `TransferSnsTreasuryFunds`.

### Finding Description

The `TokenProposalAction` trait defines `recent_amount_total_upper_bound_tokens` to cap the total amount that can be minted or transferred within a 7-day rolling window. For `TransferSnsTreasuryFunds`, this is correctly implemented and enforced. For `MintSnsTokens`, the real implementation is commented out and replaced with a stub returning `Decimal::MAX`:

```rust
// TODO(NNS1-2982): Delete.
fn recent_amount_total_upper_bound_tokens(_valuation: &Valuation) -> Result<Decimal, String> {
    Ok(Decimal::MAX)
}
```

The enforcement logic in `treasury_valuation_if_proposal_amount_is_small_enough_or_err` computes:

```rust
let max_tokens = MyTokenProposalAction::recent_amount_total_upper_bound_tokens(&valuation)?;
// ...
if proposal_amount_tokens > allowance_remainder_tokens { ... }
```

Since `max_tokens` is `Decimal::MAX`, the condition `proposal_amount_tokens > Decimal::MAX - spent_tokens` is never true for any realistic `proposal_amount_tokens`, so the guard never fires. The integration test at `rs/sns/integration_tests/src/sns_treasury.rs:942-966` explicitly confirms this: the block that asserts the second mint proposal is rejected is commented out, and `doomed_make_proposal_result.unwrap()` is used instead, proving the second unlimited mint succeeds in production.

### Impact Explanation

**Impact: High**

An SNS neuron holder with sufficient voting power can:
1. Submit and pass multiple `MintSnsTokens` proposals within a 7-day window, each minting up to `u64::MAX` e8s of SNS tokens.
2. Inflate the SNS token supply without bound, diluting all existing token holders.
3. Effectively drain value from the SNS ecosystem by minting tokens to themselves or colluding parties.

The `TransferSnsTreasuryFunds` action is correctly rate-limited (at most 25–100% of treasury value per 7 days depending on treasury size), but `MintSnsTokens` has no such limit, creating an asymmetric and exploitable gap. The `mint_sns_tokens_7_day_total_upper_bound_tokens` function in `rs/sns/governance/proposals_amount_total_limit/src/lib.rs` is fully implemented and correct — it is simply never called for `MintSnsTokens` proposals.

### Likelihood Explanation

**Likelihood: Low**

The attacker must control or coordinate enough SNS voting power to pass a `MintSnsTokens` proposal. In practice, SNS governance is designed so that no single party holds a majority, but a whale neuron holder or a coordinated group could exploit this. The vulnerability is live in production (not gated behind a feature flag), and the `TODO` comments confirm the developers are aware the limit is disabled. Any SNS where a single entity or coalition holds majority voting power is immediately exploitable.

### Recommendation

Uncomment the real implementation of `recent_amount_total_upper_bound_tokens` for `MintSnsTokens` (the block marked `TODO(NNS1-2982): Uncomment`) and delete the stub that returns `Decimal::MAX`. Simultaneously, uncomment the corresponding integration test assertion in `rs/sns/integration_tests/src/sns_treasury.rs` that verifies the second mint proposal is rejected. The correct implementation already exists in `rs/sns/governance/proposals_amount_total_limit/src/lib.rs` via `mint_sns_tokens_7_day_total_upper_bound_tokens`.

### Proof of Concept

1. Deploy an SNS where a whale neuron holds majority voting power.
2. Submit a `MintSnsTokens` proposal for `u64::MAX` e8s (≈184 billion SNS tokens).
3. Pass the proposal via the whale neuron.
4. Observe that `validate_and_render_mint_sns_tokens` calls `treasury_valuation_if_proposal_amount_is_small_enough_or_err`, which calls `MintSnsTokens::recent_amount_total_upper_bound_tokens` → `Decimal::MAX`. The guard at line 805 does not fire.
5. Repeat immediately with another proposal. The second proposal also passes, confirming no 7-day rate limit is enforced.
6. `perform_mint_sns_tokens` executes `self.ledger.transfer_funds(amount_e8s, 0, None, to, mint.memo())` with no additional amount check, minting the full requested amount.

The integration test at `rs/sns/integration_tests/src/sns_treasury.rs:966` (`doomed_make_proposal_result.unwrap()`) is the canonical proof that this succeeds in the current codebase.

---

**Root cause:** [1](#0-0) 

**Commented-out correct implementation:** [2](#0-1) 

**Enforcement logic that is bypassed:** [3](#0-2) 

**Integration test confirming the bypass:** [4](#0-3) 

**Correct upper bound function (unused for MintSnsTokens):** [5](#0-4) 

**Execution path with no amount check:** [6](#0-5)

### Citations

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
