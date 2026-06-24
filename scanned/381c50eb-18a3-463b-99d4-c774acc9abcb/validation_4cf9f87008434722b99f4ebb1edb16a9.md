### Title
`MintSnsTokens` 7-Day Upper Bound Intentionally Disabled — Unlimited SNS Token Minting via Governance Proposal - (File: `rs/sns/governance/src/proposal.rs`)

### Summary

The `MintSnsTokens` SNS governance proposal action has its 7-day minting upper bound check intentionally disabled. The `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` unconditionally returns `Decimal::MAX` instead of calling the real limit function `mint_sns_tokens_7_day_total_upper_bound_tokens`. There is also no execution-time amount check in `perform_mint_sns_tokens`, unlike the analogous `TransferSnsTreasuryFunds` action which enforces limits at both proposal submission and execution. Any SNS neuron holder who can get a `MintSnsTokens` proposal adopted can mint an unbounded quantity of SNS tokens to any principal, inflating the supply without limit.

### Finding Description

The `TokenProposalAction` trait defines `recent_amount_total_upper_bound_tokens` to cap the 7-day rolling total of token-moving proposals. For `TransferSnsTreasuryFunds`, this is properly implemented by calling `transfer_sns_treasury_funds_7_day_total_upper_bound_tokens`, which enforces tiered limits (100% of treasury for small, 25% for medium, 300,000 XDR cap for large). The same limit function exists for minting (`mint_sns_tokens_7_day_total_upper_bound_tokens`), but the `MintSnsTokens` implementation of `recent_amount_total_upper_bound_tokens` is commented out and replaced with a stub that returns `Decimal::MAX`:

```rust
// TODO(NNS1-2982): Delete.
fn recent_amount_total_upper_bound_tokens(_valuation: &Valuation) -> Result<Decimal, String> {
    Ok(Decimal::MAX)
}
``` [1](#0-0) 

The correct implementation is commented out:

```rust
/* TODO(NNS1-2982): Uncomment.
fn recent_amount_total_upper_bound_tokens(valuation: &Valuation) -> Result<Decimal, String> {
    mint_sns_tokens_7_day_total_upper_bound_tokens(*valuation) ...
}
*/
``` [2](#0-1) 

Additionally, `perform_mint_sns_tokens` performs no amount check at execution time, unlike `perform_transfer_sns_treasury_funds` which calls `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err`: [3](#0-2) 

The integration test for this scenario explicitly acknowledges the missing enforcement with a commented-out assertion and a `doomed_make_proposal_result.unwrap()` that confirms the second (should-be-rejected) proposal succeeds: [4](#0-3) 

The limit function itself is fully implemented and correct in `rs/sns/governance/proposals_amount_total_limit/src/lib.rs`: [5](#0-4) 

### Impact Explanation

A neuron holder (or coalition) with sufficient voting power in any SNS DAO can submit a `MintSnsTokens` proposal for an arbitrarily large `amount_e8s` (up to `u64::MAX`). Because `recent_amount_total_upper_bound_tokens` returns `Decimal::MAX`, the amount check in `treasury_valuation_if_proposal_amount_is_small_enough_or_err` always passes: [6](#0-5) 

Upon adoption, `perform_mint_sns_tokens` calls `self.ledger.transfer_funds(amount_e8s, 0, None, to, mint.memo())` with no further guard, minting the full requested amount. This inflates the SNS token supply without bound, diluting all existing token holders and effectively stealing their proportional share of the treasury and voting power. Multiple such proposals can be submitted and executed in rapid succession within the same 7-day window with no cumulative cap.

### Likelihood Explanation

The entry path is an ingress `manage_neuron` call from any SNS neuron holder. Many deployed SNS DAOs have concentrated voting power (founding teams, early investors) that can unilaterally pass proposals. Even in more distributed DAOs, a coalition can form. The `MintSnsTokens` proposal type is a standard, documented governance action. The disabled limit is not a configuration option — it is hardcoded in the production canister binary. The risk is present for every deployed SNS on mainnet.

### Recommendation

1. **Uncomment the real implementation** of `recent_amount_total_upper_bound_tokens` for `MintSnsTokens` (remove the `Decimal::MAX` stub and uncomment the `mint_sns_tokens_7_day_total_upper_bound_tokens` call as indicated by `TODO(NNS1-2982)`).
2. **Add an execution-time check** in `perform_mint_sns_tokens` analogous to `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err`, so that even if the treasury valuation changes between proposal submission and execution, the limit is still enforced.
3. **Uncomment the integration test assertion** in `sns_can_mint_funds_via_proposals` that verifies the second proposal is rejected.

### Proof of Concept

1. Deploy or use any existing SNS with a neuron holding majority voting power.
2. Submit a `MintSnsTokens` proposal with `amount_e8s = u64::MAX` targeting an attacker-controlled principal.
3. Vote the proposal in (or wait for followees to adopt it).
4. Observe that `perform_mint_sns_tokens` executes without any amount guard, minting `u64::MAX` e8s of SNS tokens to the attacker.
5. Repeat immediately — there is no 7-day rolling cap enforced, so the same can be done again in the same window.

The existing test at `rs/sns/integration_tests/src/sns_treasury.rs` line 966 already demonstrates this: `doomed_make_proposal_result.unwrap()` — a proposal that should be rejected by the limit passes, and the whale's balance doubles (`2 * 2_222` tokens) instead of being blocked. [7](#0-6)

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

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L14-18)
```rust
pub fn mint_sns_tokens_7_day_total_upper_bound_tokens(
    valuation: Valuation,
) -> Result<Decimal, ProposalsAmountTotalLimitError> {
    ProposalsAmountTotalUpperBound::in_tokens(valuation)
}
```
