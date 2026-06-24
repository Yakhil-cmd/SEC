### Title
Disabled 7-Day Minting Cap for `MintSnsTokens` Proposals Allows Unbounded SNS Token Inflation - (File: `rs/sns/governance/src/proposal.rs`)

---

### Summary
The `MintSnsTokens` SNS governance proposal action has its treasury-valuation-based 7-day minting cap intentionally disabled in production code. The real limit function is commented out and replaced with `Decimal::MAX`, meaning any SNS governance majority can pass unlimited successive `MintSnsTokens` proposals with no rate-limit enforcement, inflating the SNS token supply without bound.

---

### Finding Description
The `TokenProposalAction` trait defines `recent_amount_total_upper_bound_tokens` to enforce a 7-day rolling cap on how many tokens can be minted or transferred via governance proposals. For `TransferSnsTreasuryFunds`, this cap is properly wired to `transfer_sns_treasury_funds_7_day_total_upper_bound_tokens`, which computes a treasury-valuation-scaled limit.

For `MintSnsTokens`, however, the real implementation is commented out under `TODO(NNS1-2982): Uncomment`, and the active stub unconditionally returns `Decimal::MAX`:

```rust
// TODO(NNS1-2982): Delete.
fn recent_amount_total_upper_bound_tokens(_valuation: &Valuation) -> Result<Decimal, String> {
    Ok(Decimal::MAX)
}
``` [1](#0-0) 

The import for `mint_sns_tokens_7_day_total_upper_bound_tokens` is also commented out at the top of the file: [2](#0-1) 

The validation path `validate_and_render_mint_sns_tokens` calls `treasury_valuation_if_proposal_amount_is_small_enough_or_err`, which in turn calls `recent_amount_total_upper_bound_tokens`. Because that function returns `Decimal::MAX`, the check `proposal_amount_tokens > allowance_remainder_tokens` is always false, and no proposal is ever rejected for exceeding the minting cap. [3](#0-2) 

The integration test `sns_can_mint_funds_via_proposals` explicitly confirms this: the block that asserts the second minting proposal fails is commented out, and the test instead calls `.unwrap()` on what should be a rejected proposal: [4](#0-3) 

Execution of an approved `MintSnsTokens` proposal calls `perform_mint_sns_tokens`, which directly calls `transfer_funds` on the SNS ledger with no additional cap check at execution time: [5](#0-4) 

---

### Impact Explanation
Any SNS governance majority can pass repeated `MintSnsTokens` proposals with arbitrarily large `amount_e8s` values, minting SNS tokens without any 7-day rolling cap. This inflates the SNS token supply without bound, diluting all existing token holders. The `MintSnsTokens` action is a first-class governance proposal type (Id = 12) available to all SNS instances on mainnet. [6](#0-5) 

---

### Likelihood Explanation
Any SNS whose governance is controlled by a concentrated neuron holder (e.g., a whale or a colluding group) can exploit this immediately. The `MintSnsTokens` proposal type is live on mainnet for all SNS DAOs. The disabled cap is not a temporary outage — it is the active production code path, confirmed by the integration test explicitly calling `.unwrap()` on what should be a rejected proposal.

---

### Recommendation
Uncomment the real `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` (tracked as `TODO(NNS1-2982)`) and uncomment the import of `mint_sns_tokens_7_day_total_upper_bound_tokens`. The correct implementation already exists in `rs/sns/governance/proposals_amount_total_limit/src/lib.rs`: [7](#0-6) 

Until that is done, the 7-day minting cap for `MintSnsTokens` proposals is entirely unenforced.

---

### Proof of Concept

1. Deploy any SNS on mainnet.
2. Submit a `MintSnsTokens` proposal with `amount_e8s = u64::MAX` (the maximum possible).
3. Pass the proposal through normal governance voting.
4. Observe that `validate_and_render_mint_sns_tokens` calls `recent_amount_total_upper_bound_tokens`, which returns `Decimal::MAX`, so the amount check always passes.
5. `perform_mint_sns_tokens` executes, minting `u64::MAX` e8s of SNS tokens to the target account.
6. Repeat immediately — there is no cooldown or cap enforced. The SNS token supply is inflated without limit. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/sns/governance/src/proposal.rs (L51-54)
```rust
use ic_sns_governance_proposals_amount_total_limit::{
    // TODO(NNS1-2982): Uncomment. mint_sns_tokens_7_day_total_upper_bound_tokens,
    transfer_sns_treasury_funds_7_day_total_upper_bound_tokens,
};
```

**File:** rs/sns/governance/src/proposal.rs (L792-814)
```rust
    // From valuation, determine limit on the total from the past 7 days.
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

**File:** rs/sns/governance/src/proposal.rs (L875-930)
```rust
async fn validate_and_render_mint_sns_tokens(
    mint_sns_tokens: &MintSnsTokens,
    sns_transfer_fee_e8s: u64,
    env: &dyn Environment,
    swap_canister_id: CanisterId,
    sns_ledger_canister_id: CanisterId,
    proposals: impl Iterator<Item = &ProposalData>,
) -> Result<
    (
        String, // Rendering.
        ActionAuxiliary,
    ),
    String,
> {
    let mut defects = vec![];

    // Validate amount. (This requires calling CMC and the swap canister; hence, await.)
    let valuation = treasury_valuation_if_proposal_amount_is_small_enough_or_err(
        env,
        sns_ledger_canister_id,
        swap_canister_id,
        proposals,
        mint_sns_tokens,
    )
    .await;
    let valuation = match valuation {
        Ok(ok) => Some(ok),
        Err(err) => {
            defects.push(err);
            None
        }
    };

    locally_validate_and_render_mint_sns_tokens(mint_sns_tokens, sns_transfer_fee_e8s, defects)
        .and_then(|rendering| {
            match valuation {
                Some(valuation) => Ok((rendering, ActionAuxiliary::MintSnsTokens(valuation))),

                // Proof that this never happens:
                //
                //   1. valuation = None means that amount_result was Err.
                //
                //   2. In that case, nonempty defects was passed to
                //      locally_validate_and_render_mint_sns_tokens.
                //
                //   3. In that case, the function always returns Err.
                //
                //   4. Then, this closure doesn't get called.
                None => Err(
                    "There is a bug in the amount validator. Somehow, no valuation, \
                     even though a rendering was generated."
                        .to_string(),
                ),
            }
        })
}
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

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L704-708)
```text
    // Mint SNS tokens to an account.
    //
    // Id = 12.
    MintSnsTokens mint_sns_tokens = 16;

```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L14-18)
```rust
pub fn mint_sns_tokens_7_day_total_upper_bound_tokens(
    valuation: Valuation,
) -> Result<Decimal, ProposalsAmountTotalLimitError> {
    ProposalsAmountTotalUpperBound::in_tokens(valuation)
}
```
