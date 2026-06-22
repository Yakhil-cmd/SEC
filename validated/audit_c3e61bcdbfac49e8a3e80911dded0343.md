### Title
`MintSnsTokens` 7-Day Minting Cap Unenforced Due to `Decimal::MAX` Upper Bound Placeholder - (`rs/sns/governance/src/proposal.rs`)

---

### Summary

The `MintSnsTokens` governance proposal action in SNS Governance has its treasury-valuation-based 7-day minting cap completely disabled. The `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` unconditionally returns `Decimal::MAX` instead of the intended treasury-proportional limit, allowing any amount of SNS tokens to be minted via governance proposals without restriction.

---

### Finding Description

The SNS Governance canister enforces a 7-day rolling cap on token-affecting proposals (`TransferSnsTreasuryFunds` and `MintSnsTokens`) via the `TokenProposalAction` trait. The cap is computed in `treasury_valuation_if_proposal_amount_is_small_enough_or_err` by calling `recent_amount_total_upper_bound_tokens` on the action type and comparing the proposal amount against the remaining allowance.

For `TransferSnsTreasuryFunds`, this is correctly wired to `transfer_sns_treasury_funds_7_day_total_upper_bound_tokens(valuation)`, which returns a treasury-proportional limit (e.g., 25% of treasury value within 7 days for a "medium" treasury).

For `MintSnsTokens`, the correct implementation is **commented out** and replaced with a placeholder that returns `Decimal::MAX`:

```rust
// TODO(NNS1-2982): Delete.
fn recent_amount_total_upper_bound_tokens(_valuation: &Valuation) -> Result<Decimal, String> {
    Ok(Decimal::MAX)
}
```

The correct implementation that should be active is:

```rust
/* TODO(NNS1-2982): Uncomment.
fn recent_amount_total_upper_bound_tokens(valuation: &Valuation) -> Result<Decimal, String> {
    mint_sns_tokens_7_day_total_upper_bound_tokens(*valuation)
        ...
}
*/
```

The import for `mint_sns_tokens_7_day_total_upper_bound_tokens` is also commented out:

```rust
use ic_sns_governance_proposals_amount_total_limit::{
    // TODO(NNS1-2982): Uncomment. mint_sns_tokens_7_day_total_upper_bound_tokens,
    transfer_sns_treasury_funds_7_day_total_upper_bound_tokens,
};
```

Because `max_tokens = Decimal::MAX`, the check `proposal_amount_tokens > allowance_remainder_tokens` at line 805 can never be true (since `allowance_remainder_tokens = Decimal::MAX - spent_tokens ≈ Decimal::MAX`), and every `MintSnsTokens` proposal passes validation regardless of amount.

The integration test `sns_can_mint_funds_via_proposals` in `rs/sns/integration_tests/src/sns_treasury.rs` explicitly confirms this: the assertion that the second mint proposal should be **rejected** is commented out, and the test instead asserts that it **succeeds** (line 966: `doomed_make_proposal_result.unwrap()`), with the comment `// TODO(NNS1-2982): Delete this line.`

---

### Impact Explanation

**Vulnerability class: governance authorization bug / ledger conservation bug.**

The 7-day minting cap is a protocol-level safety mechanism designed to prevent even a governance majority from inflating the SNS token supply beyond a treasury-proportional limit within a rolling 7-day window. With the cap disabled:

- A whale neuron holder (or coordinated majority) can submit and pass `MintSnsTokens` proposals to mint an **unbounded** number of SNS tokens to any account within a 7-day window.
- This directly inflates the token supply, dilutes all existing token holders, and can be used to acquire dominant voting power by minting tokens to attacker-controlled accounts.
- The `perform_mint_sns_tokens` execution path at `rs/sns/governance/src/governance.rs:3062-3088` calls `self.ledger.transfer_funds(amount_e8s, 0, None, to, mint.memo())` with no additional cap check at execution time — the only cap enforcement point is the disabled validation.

---

### Likelihood Explanation

Any SNS where a single principal or coordinated group holds >50% of voting power can exploit this immediately. This is a realistic scenario for:

- Early-stage SNS deployments where the founding team retains a majority stake.
- SNS DAOs with low neuron participation where a whale can pass proposals unilaterally.
- An attacker who acquires a majority stake through secondary market purchases.

The entry path is fully unprivileged: any principal can call the SNS Governance `make_proposal` endpoint with a `MintSnsTokens` action. No admin key, leaked secret, or threshold corruption is required — only sufficient voting power to pass the proposal.

---

### Recommendation

Uncomment the correct `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` and the corresponding import of `mint_sns_tokens_7_day_total_upper_bound_tokens`, and remove the `Decimal::MAX` placeholder, as indicated by the `TODO(NNS1-2982)` markers in the code.

---

### Proof of Concept

**Root cause — disabled cap (placeholder returns `Decimal::MAX`):** [1](#0-0) 

**Correct implementation that is commented out:** [2](#0-1) 

**Commented-out import of the correct limit function:** [3](#0-2) 

**Cap check that is bypassed (always passes because `max_tokens = Decimal::MAX`):** [4](#0-3) 

**Integration test confirming the bug — second mint proposal that should be rejected is accepted:** [5](#0-4) 

**Execution path — no cap check at execution time:** [6](#0-5)

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
