### Title
`MintSnsTokens` 7-Day Minting Cap Permanently Disabled — Unlimited SNS Token Issuance Bypasses Supply Protection - (File: `rs/sns/governance/src/proposal.rs`)

### Summary

The SNS governance canister's `MintSnsTokens` proposal action has its 7-day minting upper-bound check intentionally disabled via a `TODO` comment. The `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` unconditionally returns `Decimal::MAX` instead of the treasury-valuation-based cap. Any SNS governance participant with sufficient voting power can pass an unlimited number of `MintSnsTokens` proposals within a 7-day window, minting an unbounded quantity of SNS tokens with no protocol-level enforcement.

### Finding Description

The `TokenProposalAction` trait defines `recent_amount_total_upper_bound_tokens` as the mechanism that caps how many tokens can be minted (or transferred) within a rolling 7-day window. For `TransferSnsTreasuryFunds`, this cap is fully enforced via `transfer_sns_treasury_funds_7_day_total_upper_bound_tokens`. For `MintSnsTokens`, the correct implementation is commented out and replaced with a stub that returns `Decimal::MAX`:

```rust
// TODO(NNS1-2982): Delete.
fn recent_amount_total_upper_bound_tokens(_valuation: &Valuation) -> Result<Decimal, String> {
    Ok(Decimal::MAX)
}
```

The correct implementation exists but is blocked behind a comment:

```rust
/* TODO(NNS1-2982): Uncomment.
fn recent_amount_total_upper_bound_tokens(valuation: &Valuation) -> Result<Decimal, String> {
    mint_sns_tokens_7_day_total_upper_bound_tokens(*valuation) ...
}
*/
```

The enforcement function `treasury_valuation_if_proposal_amount_is_small_enough_or_err` computes `allowance_remainder_tokens = max_tokens - spent_tokens` and rejects proposals where `proposal_amount_tokens > allowance_remainder_tokens`. Because `max_tokens` is `Decimal::MAX`, the check `proposal_amount_tokens > Decimal::MAX` is always `false`, so every `MintSnsTokens` proposal passes regardless of how many tokens have already been minted in the past 7 days.

The integration test at `rs/sns/integration_tests/src/sns_treasury.rs` line 966 explicitly confirms this: the assertion that a second mint should fail is commented out, and `doomed_make_proposal_result.unwrap()` is called instead — the second mint succeeds when it should be rejected.

### Impact Explanation

An SNS governance participant (any principal with enough voting power, or a coalition of neurons) can submit and pass back-to-back `MintSnsTokens` proposals within a 7-day window, minting an arbitrary quantity of SNS tokens. This:

1. **Dilutes existing token holders** without bound, undermining token economics.
2. **Inflates total SNS token supply**, which affects governance quorum calculations since quorum thresholds (`minimum_yes_proportion_of_total`) are fractions of total voting power. If minted tokens are staked into neurons, the total voting power grows, raising the absolute vote count needed to pass future proposals.
3. **Drains SNS treasury value** by inflating supply, harming all token holders.

### Likelihood Explanation

Any SNS governance participant who can pass a `MintSnsTokens` proposal (i.e., has or can assemble the required voting power) can exploit this immediately. The bypass requires no special privileges beyond normal governance participation. The integration test confirms the bypass is reachable and functional in the current codebase.

### Recommendation

Uncomment the correct `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` and delete the `Decimal::MAX` stub. Also uncomment the corresponding integration test assertion in `rs/sns/integration_tests/src/sns_treasury.rs` that verifies the second mint is rejected.

```rust
// In rs/sns/governance/src/proposal.rs, replace:
fn recent_amount_total_upper_bound_tokens(_valuation: &Valuation) -> Result<Decimal, String> {
    Ok(Decimal::MAX)
}

// With:
fn recent_amount_total_upper_bound_tokens(valuation: &Valuation) -> Result<Decimal, String> {
    mint_sns_tokens_7_day_total_upper_bound_tokens(*valuation)
        .map_err(|treasury_limit_error| {
            format!("Unable to validate amount: {:?}", treasury_limit_error)
        })
}
```

Also uncomment `mint_sns_tokens_7_day_total_upper_bound_tokens` in the import at line 52 of `rs/sns/governance/src/proposal.rs`.

### Proof of Concept

The existing integration test at `rs/sns/integration_tests/src/sns_treasury.rs` already demonstrates the bypass. The test submits a first `MintSnsTokens` proposal for 2,222 SNS tokens, which executes successfully. It then submits a second identical proposal. The commented-out block (lines 942–965) shows the **expected** behavior: the second proposal should be rejected with `InvalidProposal` citing "amount too large / upper bound exceeded." Instead, line 966 calls `doomed_make_proposal_result.unwrap()`, confirming the second mint **succeeds** — the cap is not enforced.

Entry path:
1. Attacker holds or assembles sufficient SNS neuron voting power.
2. Attacker submits `MintSnsTokens` proposal via `manage_neuron` → `MakeProposal`.
3. `validate_and_render_mint_sns_tokens` calls `treasury_valuation_if_proposal_amount_is_small_enough_or_err`, which calls `MintSnsTokens::recent_amount_total_upper_bound_tokens` → returns `Decimal::MAX`.
4. The check `proposal_amount_tokens > Decimal::MAX` is always `false`; proposal is accepted.
5. Proposal executes, minting tokens. Attacker repeats indefinitely within 7 days. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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
