### Title
`MintSnsTokens` 7-Day Rate Limit Permanently Disabled Allows Unbounded SNS Token Minting - (`rs/sns/governance/src/proposal.rs`)

### Summary

The SNS Governance `MintSnsTokens` proposal action has its intended 7-day rolling rate limit deliberately disabled via a stub that returns `Decimal::MAX` as the upper bound. The analogous `TransferSnsTreasuryFunds` action enforces a proper treasury-size-proportional rate limit both at proposal submission and at execution time. `MintSnsTokens` enforces neither, allowing an SNS governance majority to mint an unbounded quantity of SNS tokens to any address in a single 7-day window.

### Finding Description

The `TokenProposalAction` trait for `MintSnsTokens` in `rs/sns/governance/src/proposal.rs` has its `recent_amount_total_upper_bound_tokens` method returning `Decimal::MAX` — effectively no limit — with the real implementation commented out behind a `TODO(NNS1-2982)`: [1](#0-0) 

The commented-out implementation would call `mint_sns_tokens_7_day_total_upper_bound_tokens`, which enforces the same treasury-size-proportional limits as `TransferSnsTreasuryFunds` (e.g., 25% of treasury per 7 days for medium-sized treasuries, capped at 300,000 XDR for large ones): [2](#0-1) 

The `TransferSnsTreasuryFunds` execution path calls `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` as a second enforcement gate: [3](#0-2) 

`perform_mint_sns_tokens` has no analogous execution-time check — it proceeds directly to `ledger.transfer_funds`: [4](#0-3) 

The integration test `sns_can_mint_funds_via_proposals` explicitly confirms the missing limit is live in production: the second mint proposal (which should be rejected) is asserted to succeed with `doomed_make_proposal_result.unwrap()`, and the expected rejection assertion is commented out behind the same `TODO(NNS1-2982)`: [5](#0-4) 

The import line in `proposal.rs` also confirms `mint_sns_tokens_7_day_total_upper_bound_tokens` is commented out of the active import: [6](#0-5) 

### Impact Explanation

An SNS governance majority can submit and pass any number of `MintSnsTokens` proposals in rapid succession, each minting up to `u64::MAX` e8s of SNS tokens to an attacker-controlled address. Because there is no per-proposal cap, no 7-day rolling cap, and no execution-time guard, the total minted amount is bounded only by the SNS ledger's arithmetic limits. This enables:

- **Token supply inflation**: Minting tokens to attacker-controlled addresses dilutes all existing SNS token holders proportionally.
- **Governance takeover**: Minting enough tokens to a single address can flip the voting majority, permanently capturing the SNS DAO.
- **Treasury drain by proxy**: Once governance is captured via minted tokens, `TransferSnsTreasuryFunds` proposals can drain the treasury (the rate limit on that action is bypassed by first capturing governance).

### Likelihood Explanation

Many SNS deployments launch with a single founding team or whale neuron controlling the majority of voting power. The vulnerability requires only that a governance majority exists and is willing to act — a realistic scenario for early-stage or poorly-distributed SNS DAOs. The code itself acknowledges the missing limit is a known, tracked deficiency (`TODO(NNS1-2982)`), confirming it is not a design choice but an incomplete implementation currently deployed on mainnet.

### Recommendation

1. Uncomment the real `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` in `rs/sns/governance/src/proposal.rs` (lines 1025–1033) and delete the stub (lines 1035–1041).
2. Add an execution-time rate-limit check inside `perform_mint_sns_tokens` in `rs/sns/governance/src/governance.rs`, analogous to the `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` call in `perform_transfer_sns_treasury_funds`.
3. Uncomment the corresponding assertion in the integration test `sns_can_mint_funds_via_proposals` (lines 942–965 of `rs/sns/integration_tests/src/sns_treasury.rs`) to prevent regression.

### Proof of Concept

1. An SNS is deployed where a single whale neuron controls >50% of voting power.
2. Whale submits `MintSnsTokens { amount_e8s: Some(u64::MAX), to_principal: Some(whale_principal), ... }`.
3. `validate_and_render_mint_sns_tokens` calls `treasury_valuation_if_proposal_amount_is_small_enough_or_err`, which calls `recent_amount_total_upper_bound_tokens` → returns `Decimal::MAX`; the amount check passes unconditionally.
4. Whale votes yes; proposal is adopted.
5. `perform_mint_sns_tokens` executes with no execution-time guard, calling `self.ledger.transfer_funds(u64::MAX, 0, None, whale_account, ...)`.
6. Whale receives `u64::MAX` e8s of SNS tokens, instantly becoming the overwhelming governance majority and diluting all other holders to near-zero.

### Citations

**File:** rs/sns/governance/src/proposal.rs (L51-54)
```rust
use ic_sns_governance_proposals_amount_total_limit::{
    // TODO(NNS1-2982): Uncomment. mint_sns_tokens_7_day_total_upper_bound_tokens,
    transfer_sns_treasury_funds_7_day_total_upper_bound_tokens,
};
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

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L14-18)
```rust
pub fn mint_sns_tokens_7_day_total_upper_bound_tokens(
    valuation: Valuation,
) -> Result<Decimal, ProposalsAmountTotalLimitError> {
    ProposalsAmountTotalUpperBound::in_tokens(valuation)
}
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
