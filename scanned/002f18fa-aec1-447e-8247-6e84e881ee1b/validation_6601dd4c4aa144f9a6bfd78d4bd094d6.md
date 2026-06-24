### Title
Unbounded `MintSnsTokens` Proposal Amount — No Effective 7-Day Minting Cap Enforced - (File: `rs/sns/governance/src/proposal.rs`)

---

### Summary

The `MintSnsTokens` governance proposal action in SNS Governance intentionally has its 7-day minting upper-bound check **disabled** via a `TODO` comment. The `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` returns `Decimal::MAX` instead of the treasury-valuation-based cap, meaning any neuron holder with sufficient voting power can pass repeated `MintSnsTokens` proposals that mint an unbounded quantity of SNS tokens — analogous to the reported bug where a caller could set `airdropAmount` to any value up to the maximum available, drawing all available liquidity.

---

### Finding Description

In `rs/sns/governance/src/proposal.rs`, the `TokenProposalAction` trait implementation for `MintSnsTokens` contains a deliberately disabled upper-bound check:

```rust
/* TODO(NNS1-2982): Uncomment.
fn recent_amount_total_upper_bound_tokens(valuation: &Valuation) -> Result<Decimal, String> {
    mint_sns_tokens_7_day_total_upper_bound_tokens(*valuation)
        ...
}
*/

// TODO(NNS1-2982): Delete.
fn recent_amount_total_upper_bound_tokens(_valuation: &Valuation) -> Result<Decimal, String> {
    Ok(Decimal::MAX)
}
``` [1](#0-0) 

The `treasury_valuation_if_proposal_amount_is_small_enough_or_err` function computes `max_tokens` from `recent_amount_total_upper_bound_tokens` and then checks `proposal_amount_tokens > allowance_remainder_tokens`. Because `max_tokens = Decimal::MAX`, the check `proposal_amount_tokens > Decimal::MAX` is always `false`, so **no amount is ever rejected**. [2](#0-1) 

The correct implementation — `mint_sns_tokens_7_day_total_upper_bound_tokens` — exists in `rs/sns/governance/proposals_amount_total_limit/src/lib.rs` and is even imported (commented out) at the top of `proposal.rs`:

```rust
// TODO(NNS1-2982): Uncomment. mint_sns_tokens_7_day_total_upper_bound_tokens,
``` [3](#0-2) 

The integration test `sns_can_mint_funds_via_proposals` explicitly confirms this: the second proposal that should be rejected by the minting limit is instead allowed to succeed, and the test comment says `doomed_make_proposal_result.unwrap(); // TODO(NNS1-2982): Delete this line.` [4](#0-3) 

At execution time, `perform_mint_sns_tokens` calls `self.ledger.transfer_funds(amount_e8s, 0, None, to, mint.memo())` with no additional amount cap — it simply mints whatever `amount_e8s` was approved in the proposal. [5](#0-4) 

By contrast, `TransferSnsTreasuryFunds` has its cap **fully enforced** both at proposal submission and at execution time via `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err`. [6](#0-5) 

---

### Impact Explanation

An SNS neuron holder (or coalition) with sufficient voting power can submit and pass repeated `MintSnsTokens` proposals with `amount_e8s = u64::MAX` (≈ 184 billion tokens) in rapid succession within a 7-day window, with no protocol-level rejection. This inflates the SNS token supply without bound, diluting all existing token holders and draining the economic value of the SNS treasury. The minting is irreversible on-chain. This is a **governance authorization / ledger conservation bug**: the amount parameter is caller-controlled (via proposal) and is not bounded by any maximum, directly analogous to the reported `airdropAmount` issue.

---

### Likelihood Explanation

Any SNS with a sufficiently concentrated voting power distribution (e.g., a whale neuron or a small set of colluding neurons) can exploit this immediately. The `MintSnsTokens` proposal type is a standard, publicly documented SNS governance action reachable by any neuron holder who can achieve proposal adoption. No privileged key or admin access is required beyond normal governance participation. The disabled check is a known, tracked issue (`NNS1-2982`) that has not yet been resolved in this codebase snapshot.

---

### Recommendation

1. **Uncomment** the real `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` that calls `mint_sns_tokens_7_day_total_upper_bound_tokens(*valuation)`, and **delete** the temporary `Decimal::MAX` stub.
2. **Uncomment** the import of `mint_sns_tokens_7_day_total_upper_bound_tokens` at the top of `proposal.rs`.
3. **Add** an execution-time re-check analogous to `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` for `MintSnsTokens`, so that the cap is enforced even if two proposals were submitted concurrently before either was executed.
4. **Update** the integration test `sns_can_mint_funds_via_proposals` to assert that the second proposal is rejected (uncomment the `TODO(NNS1-2982)` block and delete the `unwrap()` workaround line).

---

### Proof of Concept

1. Deploy an SNS with a single whale neuron controlling >50% voting power.
2. Submit `MintSnsTokens { amount_e8s: Some(u64::MAX), to_principal: Some(whale_principal), ... }`.
3. The proposal passes `validate_and_render_mint_sns_tokens` because `treasury_valuation_if_proposal_amount_is_small_enough_or_err` computes `max_tokens = Decimal::MAX`, so `proposal_amount_tokens > Decimal::MAX` is always `false`.
4. The whale votes to adopt; `perform_mint_sns_tokens` calls `ledger.transfer_funds(u64::MAX, 0, None, whale_account, 0)`, minting ~184 billion tokens to the whale.
5. Repeat immediately — no 7-day window is enforced. The SNS token supply is inflated without limit.

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

**File:** rs/sns/governance/src/governance.rs (L3000-3005)
```rust
        transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err(
            transfer,
            valuation?,
            self.proto.proposals.values(),
            self.env.now(),
        )?;
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
