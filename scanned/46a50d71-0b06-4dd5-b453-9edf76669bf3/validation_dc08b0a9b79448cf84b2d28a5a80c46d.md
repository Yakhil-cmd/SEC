### Title
SNS `MintSnsTokens` Proposal Action Has No Effective 7-Day Upper Bound, Enabling Unlimited Token Inflation - (File: `rs/sns/governance/src/proposal.rs`)

---

### Summary

The `MintSnsTokens` SNS governance proposal action has its 7-day minting upper bound check intentionally disabled — returning `Decimal::MAX` instead of calling the real limit function — while the analogous `TransferSnsTreasuryFunds` action correctly enforces a treasury-proportional cap. Any SNS governance majority (including a founding team with initial majority voting power) can pass back-to-back `MintSnsTokens` proposals to inflate the SNS token supply without restriction.

---

### Finding Description

The `TokenProposalAction` trait in `rs/sns/governance/src/proposal.rs` defines `recent_amount_total_upper_bound_tokens` to cap how many tokens can be minted or transferred within a rolling 7-day window.

For `TransferSnsTreasuryFunds`, the bound is properly enforced:

```rust
fn recent_amount_total_upper_bound_tokens(valuation: &Valuation) -> Result<Decimal, String> {
    transfer_sns_treasury_funds_7_day_total_upper_bound_tokens(*valuation)
        .map_err(|treasury_limit_error| {
            format!("Unable to validate amount: {treasury_limit_error:?}",)
        })
}
``` [1](#0-0) 

For `MintSnsTokens`, the real bound function (`mint_sns_tokens_7_day_total_upper_bound_tokens`) is commented out with a `TODO(NNS1-2982)` marker, and replaced with a stub that unconditionally returns `Decimal::MAX`:

```rust
// TODO(NNS1-2982): Delete.
fn recent_amount_total_upper_bound_tokens(_valuation: &Valuation) -> Result<Decimal, String> {
    Ok(Decimal::MAX)
}
``` [2](#0-1) 

The import of `mint_sns_tokens_7_day_total_upper_bound_tokens` is also commented out at the top of the file:

```rust
use ic_sns_governance_proposals_amount_total_limit::{
    // TODO(NNS1-2982): Uncomment. mint_sns_tokens_7_day_total_upper_bound_tokens,
    transfer_sns_treasury_funds_7_day_total_upper_bound_tokens,
};
``` [3](#0-2) 

The validation path for `MintSnsTokens` proposals calls `treasury_valuation_if_proposal_amount_is_small_enough_or_err`, which calls `recent_amount_total_upper_bound_tokens` and then checks:

```rust
if proposal_amount_tokens > allowance_remainder_tokens { ... }
``` [4](#0-3) 

Because `allowance_remainder_tokens = Decimal::MAX - spent_tokens ≈ Decimal::MAX`, this check **never triggers** for any realistic `proposal_amount_tokens`. The real limit function exists and is ready to use:

```rust
pub fn mint_sns_tokens_7_day_total_upper_bound_tokens(
    valuation: Valuation,
) -> Result<Decimal, ProposalsAmountTotalLimitError> {
    ProposalsAmountTotalUpperBound::in_tokens(valuation)
}
``` [5](#0-4) 

The integration test that should verify the second mint is rejected is also commented out, confirming the protection is not active:

```rust
/* TODO(NNS1-2982): Uncomment.
let err = doomed_make_proposal_result.unwrap_err();
...
*/
doomed_make_proposal_result.unwrap(); // TODO(NNS1-2982): Delete this line.
``` [6](#0-5) 

---

### Impact Explanation

An SNS governance majority — which in the early life of any SNS is typically the founding team holding a supermajority of voting power — can submit and pass `MintSnsTokens` proposals of arbitrary size, repeatedly, with no 7-day rate limit. This allows:

- **Unlimited SNS token inflation**: existing token holders are diluted without bound.
- **Rug-pull via governance**: the founding team can present to their community that minting is limited (the limit infrastructure exists and is visible in the code), then exploit the disabled check to mint tokens at will after the community has invested.

The `ProposalsAmountTotalUpperBound` logic in `rs/sns/governance/proposals_amount_total_limit/src/lib.rs` caps treasury-proportional actions at 100% of treasury for small SNSes, 25% for medium, and 300,000 XDR for large ones within 7 days. [7](#0-6) 

None of these caps apply to `MintSnsTokens` in the current code.

---

### Likelihood Explanation

- The `MintSnsTokens` proposal action is a live, callable SNS governance feature exposed via the SNS governance canister's public Candid interface. [8](#0-7) 
- Any SNS whose founding team retains majority voting power (common in early-stage SNSes) can exploit this immediately.
- No special key, leaked credential, or consensus-layer attack is required — only a standard governance proposal submission and vote.
- The asymmetry with `TransferSnsTreasuryFunds` (which is properly limited) means users and auditors may incorrectly assume `MintSnsTokens` is equally constrained.

---

### Recommendation

Uncomment the real upper bound implementation for `MintSnsTokens`:

```rust
// In rs/sns/governance/src/proposal.rs
fn recent_amount_total_upper_bound_tokens(valuation: &Valuation) -> Result<Decimal, String> {
    mint_sns_tokens_7_day_total_upper_bound_tokens(*valuation)
        .map_err(|treasury_limit_error| {
            format!("Unable to validate amount: {:?}", treasury_limit_error,)
        })
}
```

And restore the import:

```rust
use ic_sns_governance_proposals_amount_total_limit::{
    mint_sns_tokens_7_day_total_upper_bound_tokens,
    transfer_sns_treasury_funds_7_day_total_upper_bound_tokens,
};
```

Also uncomment the corresponding integration test assertion in `rs/sns/integration_tests/src/sns_treasury.rs` to enforce the regression guard.

---

### Proof of Concept

1. An SNS founding team holds >50% of voting power (standard at SNS launch).
2. They submit a `MintSnsTokens` proposal with `amount_e8s = u64::MAX` (≈ 184 billion tokens).
3. The proposal passes via their majority.
4. `validate_and_render_mint_sns_tokens` is called, which calls `treasury_valuation_if_proposal_amount_is_small_enough_or_err`. [9](#0-8) 
5. `MintSnsTokens::recent_amount_total_upper_bound_tokens` returns `Decimal::MAX`. [10](#0-9) 
6. The guard `if proposal_amount_tokens > allowance_remainder_tokens` evaluates to `false` (since `allowance_remainder_tokens ≈ Decimal::MAX`). [11](#0-10) 
7. The proposal executes: `u64::MAX` SNS tokens are minted to the attacker's account, inflating the total supply by orders of magnitude.
8. Steps 2–7 can be repeated indefinitely with no cooldown.

### Citations

**File:** rs/sns/governance/src/proposal.rs (L51-54)
```rust
use ic_sns_governance_proposals_amount_total_limit::{
    // TODO(NNS1-2982): Uncomment. mint_sns_tokens_7_day_total_upper_bound_tokens,
    transfer_sns_treasury_funds_7_day_total_upper_bound_tokens,
};
```

**File:** rs/sns/governance/src/proposal.rs (L801-813)
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

**File:** rs/sns/governance/src/proposal.rs (L892-899)
```rust
    let valuation = treasury_valuation_if_proposal_amount_is_small_enough_or_err(
        env,
        sns_ledger_canister_id,
        swap_canister_id,
        proposals,
        mint_sns_tokens,
    )
    .await;
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

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L36-41)
```rust
    const MAX_SMALL_TREASURY_SIZE_XDR: Decimal = dec!(100_000);
    const MAX_MEDIUM_TREASURY_SIZE_XDR: Decimal = dec!(1_200_000);

    // No matter how large the treasury is, not more than this amount can be removed (within a 7 day
    // window).
    const MAX_XDR: Decimal = dec!(300_000);
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

**File:** rs/sns/governance/canister/governance.did (L527-532)
```text
type MintSnsTokens = record {
  to_principal : opt principal;
  to_subaccount : opt Subaccount;
  memo : opt nat64;
  amount_e8s : opt nat64;
};
```
