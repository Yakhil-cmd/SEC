### Title
Missing Upper Bound on `MintSnsTokens` 7-Day Minting Amount Allows Unbounded SNS Token Inflation - (`File: rs/sns/governance/src/proposal.rs`)

---

### Summary

The `MintSnsTokens` proposal action in SNS Governance intentionally bypasses the 7-day minting upper-bound check by returning `Decimal::MAX` as the allowed limit. This is an acknowledged temporary stub (marked `TODO(NNS1-2982): Delete`) that was never replaced with the real cap. Any SNS community with sufficient voting power can pass repeated `MintSnsTokens` proposals to mint an unbounded quantity of SNS tokens within any 7-day window, with no protocol-enforced ceiling analogous to the one that already exists for `TransferSnsTreasuryFunds`.

---

### Finding Description

The `TokenProposalAction` trait defines `recent_amount_total_upper_bound_tokens` to cap how many tokens can be minted or transferred in a rolling 7-day window. For `TransferSnsTreasuryFunds`, the real implementation calls `transfer_sns_treasury_funds_7_day_total_upper_bound_tokens`, which enforces a tiered cap (100% of treasury for small, 25% for medium, 300,000 XDR absolute for large).

For `MintSnsTokens`, the real implementation is commented out and replaced with a stub that unconditionally returns `Decimal::MAX`:

```rust
// TODO(NNS1-2982): Delete.
fn recent_amount_total_upper_bound_tokens(_valuation: &Valuation) -> Result<Decimal, String> {
    // Ideally, we'd return infinity, but Decimal does not have that. This is the next best
    // thing, and should be good enough, because we have already planned the obselences of this
    // code (see tickets NNS1-298(1|2)).
    Ok(Decimal::MAX)
}
```

The check in `treasury_valuation_if_proposal_amount_is_small_enough_or_err` compares `proposal_amount_tokens > allowance_remainder_tokens`. Since `allowance_remainder_tokens` is `Decimal::MAX - 0 = Decimal::MAX`, any `u64`-bounded `amount_e8s` will always pass. The integration test that should verify the limit is also commented out:

```rust
/* TODO(NNS1-2982): Uncomment.
let err = doomed_make_proposal_result.unwrap_err();
...
*/
doomed_make_proposal_result.unwrap(); // TODO(NNS1-2982): Delete this line.
```

The real cap function `mint_sns_tokens_7_day_total_upper_bound_tokens` exists and is correct, but is never called from the production path because the import is commented out:

```rust
use ic_sns_governance_proposals_amount_total_limit::{
    // TODO(NNS1-2982): Uncomment. mint_sns_tokens_7_day_total_upper_bound_tokens,
    transfer_sns_treasury_funds_7_day_total_upper_bound_tokens,
};
```

---

### Impact Explanation

Any SNS with a sufficiently motivated token-holder majority can pass back-to-back `MintSnsTokens` proposals, each minting up to `u64::MAX` e8s of SNS tokens, with no protocol-enforced 7-day ceiling. This allows:

- **Unbounded token inflation**: The SNS token supply can be inflated arbitrarily fast, diluting all existing holders.
- **Treasury drain via inflation**: Newly minted tokens can be sent to any principal, effectively transferring value out of the SNS ecosystem without the treasury-transfer rate limit applying.
- **Asymmetric protection**: `TransferSnsTreasuryFunds` is rate-limited; `MintSnsTokens` is not. An attacker who controls an SNS governance majority can exploit this asymmetry to extract value faster than the treasury-transfer path allows.

This is the direct IC analog of the BendDAO finding: a missing maximum cap on an amount calculation means the protective limit is never enforced, allowing economically harmful actions that the protocol was designed to prevent.

---

### Likelihood Explanation

The entry path is a standard SNS governance proposal submission (`manage_neuron` → `MintSnsTokens` action), callable by any SNS neuron holder with sufficient voting power. No privileged role, admin key, or subnet-majority corruption is required. The only prerequisite is controlling enough SNS voting power to pass a proposal, which is the normal operating condition for any SNS community. The stub has been present since the feature was introduced and is explicitly marked for future deletion, meaning it is active in production today.

---

### Recommendation

1. Uncomment the real `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` (ticket NNS1-2982) and remove the `Decimal::MAX` stub.
2. Uncomment the import of `mint_sns_tokens_7_day_total_upper_bound_tokens` in `rs/sns/governance/src/proposal.rs`.
3. Uncomment the integration test assertion in `rs/sns/integration_tests/src/sns_treasury.rs` that verifies the second mint proposal is rejected.

---

### Proof of Concept

**Root cause — stub returning `Decimal::MAX`:** [1](#0-0) 

**Commented-out real implementation:** [2](#0-1) 

**Commented-out import of the real cap function:** [3](#0-2) 

**The check that is bypassed (always passes because `max_tokens = Decimal::MAX`):** [4](#0-3) 

**The real cap function that exists but is never called:** [5](#0-4) 

**Integration test assertion that is commented out, allowing the second unlimited mint to succeed:** [6](#0-5) 

**`TransferSnsTreasuryFunds` correctly uses the real cap (contrast):** [7](#0-6)

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
