### Title
`MintSnsTokens` 7-Day Minting Limit Intentionally Disabled, Allowing Unbounded SNS Token Inflation - (File: `rs/sns/governance/src/proposal.rs`)

### Summary
The SNS governance `MintSnsTokens` proposal action is designed to enforce a 7-day rolling minting limit (analogous to the `maxSupply` check in the reference report). However, the enforcement function is intentionally commented out and replaced with a stub returning `Decimal::MAX`, meaning no minting cap is enforced. Any SNS governance majority can submit unlimited `MintSnsTokens` proposals and mint an unbounded quantity of SNS tokens, diluting all token holders without restriction.

### Finding Description
The `TokenProposalAction` trait defines `recent_amount_total_upper_bound_tokens` as the mechanism to cap the 7-day rolling total of minted or transferred tokens. For `TransferSnsTreasuryFunds`, this is properly wired to `transfer_sns_treasury_funds_7_day_total_upper_bound_tokens`, which computes a real XDR-denominated cap.

For `MintSnsTokens`, the real implementation is commented out:

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

The stub returns `Decimal::MAX`, which means the check at `treasury_valuation_if_proposal_amount_is_small_enough_or_err` always passes:

```rust
if proposal_amount_tokens > allowance_remainder_tokens { ... }
``` [2](#0-1) 

The import of `mint_sns_tokens_7_day_total_upper_bound_tokens` is also commented out at the module level:

```rust
use ic_sns_governance_proposals_amount_total_limit::{
    // TODO(NNS1-2982): Uncomment. mint_sns_tokens_7_day_total_upper_bound_tokens,
    transfer_sns_treasury_funds_7_day_total_upper_bound_tokens,
};
``` [3](#0-2) 

The integration test explicitly confirms this behavior — the "doomed" second mint proposal that should be rejected by the limit actually **succeeds**, and the balance check reflects `2 × 2_222` tokens minted:

```rust
doomed_make_proposal_result.unwrap(); // TODO(NNS1-2982): Delete this line.
...
let expected_balance_tokens = Tokens::new(
    2 * // TODO(NNS1-2982): Delete this line.
    2_222, 0,
).unwrap();
``` [4](#0-3) 

The execution path for `MintSnsTokens` proposals goes through `perform_mint_sns_tokens`, which calls `transfer_funds` directly on the SNS ledger with no additional supply guard: [5](#0-4) 

### Impact Explanation
An SNS governance majority (a coalition of token holders controlling >50% of voting power) can submit an unlimited number of `MintSnsTokens` proposals in rapid succession. Each proposal mints an arbitrary amount of SNS tokens to any target account. Because the 7-day rolling cap returns `Decimal::MAX`, no proposal is ever rejected on amount grounds. This allows:
- Unlimited dilution of all existing SNS token holders
- Extraction of value from the SNS treasury indirectly (by minting tokens and selling them)
- Bypassing the intended governance safeguard that mirrors the `maxSupply` check in the reference report

The `mint_sns_tokens_7_day_total_upper_bound_tokens` function exists and is correct — it computes a proper XDR-denominated cap — but it is never called in production. [6](#0-5) 

### Likelihood Explanation
**High.** Any SNS with a concentrated token distribution (e.g., a whale neuron or coordinated group) can exploit this immediately. The code itself acknowledges the missing enforcement via `TODO(NNS1-2982)` comments. The attack requires only normal SNS governance participation — submitting and voting on proposals — which is an explicitly listed attacker role ("ledger/governance/chain-fusion user"). No privileged keys, threshold attacks, or external dependencies are needed.

### Recommendation
Uncomment the real `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` and import `mint_sns_tokens_7_day_total_upper_bound_tokens` in `rs/sns/governance/src/proposal.rs`. Delete the stub that returns `Decimal::MAX`. Update the integration test in `rs/sns/integration_tests/src/sns_treasury.rs` to assert that the second `MintSnsTokens` proposal is rejected with an `InvalidProposal` error (the commented-out assertion block at lines 942–965 is already written and ready to be uncommented).

### Proof of Concept
1. Deploy an SNS with a single whale neuron holding majority voting power.
2. Submit `MintSnsTokens` proposal #1: mint `u64::MAX / E8` SNS tokens to the whale's account. The proposal passes validation because `recent_amount_total_upper_bound_tokens` returns `Decimal::MAX`.
3. Immediately submit `MintSnsTokens` proposal #2 with the same amount. It also passes validation for the same reason — `total_minting_amount_tokens` correctly accumulates the prior executed amount, but the cap is `Decimal::MAX`, so `proposal_amount_tokens > allowance_remainder_tokens` is never true.
4. Both proposals execute via `perform_mint_sns_tokens`, minting tokens directly on the SNS ledger with no supply guard.
5. The whale now holds a disproportionate share of the SNS token supply, diluting all other holders. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/sns/governance/src/proposal.rs (L51-54)
```rust
use ic_sns_governance_proposals_amount_total_limit::{
    // TODO(NNS1-2982): Uncomment. mint_sns_tokens_7_day_total_upper_bound_tokens,
    transfer_sns_treasury_funds_7_day_total_upper_bound_tokens,
};
```

**File:** rs/sns/governance/src/proposal.rs (L770-816)
```rust
async fn treasury_valuation_if_proposal_amount_is_small_enough_or_err<MyTokenProposalAction>(
    env: &dyn Environment,
    sns_ledger_canister_id: CanisterId,
    swap_canister_id: CanisterId,
    proposals: impl Iterator<Item = &ProposalData>,
    action: &MyTokenProposalAction,
) -> Result<Valuation, String>
where
    MyTokenProposalAction: TokenProposalAction,
{
    let spent_tokens = action.recent_amount_total_tokens(proposals, env.now())?;

    // Get valuation of the tokens in the treasury.
    let token = action.token()?;
    let valuation = assess_treasury_balance(
        token,
        env.canister_id(),
        sns_ledger_canister_id,
        swap_canister_id,
    )
    .await?;

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

    Ok(valuation)
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

**File:** rs/sns/integration_tests/src/sns_treasury.rs (L966-983)
```rust
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
