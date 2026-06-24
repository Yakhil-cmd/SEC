The code evidence is conclusive. This is a real, confirmed vulnerability.

---

### Title
`MintSnsTokens` 7-Day Rate Limit Disabled via `Decimal::MAX` Upper Bound — (`rs/sns/governance/src/proposal.rs`)

### Summary

The `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` is intentionally stubbed to return `Decimal::MAX`, disabling the treasury-valuation-based 7-day minting cap entirely. The real enforcement function exists but is commented out pending ticket NNS1-2982. Any SNS governance participant with sufficient voting power can pass unlimited `MintSnsTokens` proposals within a 7-day window with no rate-limit enforcement.

### Finding Description

The `TokenProposalAction` trait defines `recent_amount_total_upper_bound_tokens` as the ceiling on how many tokens may be minted within a rolling 7-day window. For `MintSnsTokens`, the production implementation is:

```rust
// TODO(NNS1-2982): Delete.
fn recent_amount_total_upper_bound_tokens(_valuation: &Valuation) -> Result<Decimal, String> {
    Ok(Decimal::MAX)
}
``` [1](#0-0) 

The real implementation — which calls `mint_sns_tokens_7_day_total_upper_bound_tokens` from the `proposals_amount_total_limit` crate — is commented out:

```rust
/* TODO(NNS1-2982): Uncomment.
fn recent_amount_total_upper_bound_tokens(valuation: &Valuation) -> Result<Decimal, String> {
    mint_sns_tokens_7_day_total_upper_bound_tokens(*valuation)
        ...
}
*/
``` [2](#0-1) 

The guard function `treasury_valuation_if_proposal_amount_is_small_enough_or_err` computes `max_tokens - spent_tokens` and rejects proposals that exceed it — but since `max_tokens` is always `Decimal::MAX`, the check `proposal_amount_tokens > allowance_remainder_tokens` can never trigger for any realistic `u64`-bounded amount. [3](#0-2) 

The real cap function is fully implemented and would correctly compute a treasury-valuation-based limit (e.g., 25% of treasury for medium-sized treasuries, capped at 300,000 XDR): [4](#0-3) 

The integration test explicitly confirms the bypass is active — the second mint proposal (which should be rejected by the rate limit) is expected to **succeed**:

```rust
/* TODO(NNS1-2982): Uncomment.
let err = doomed_make_proposal_result.unwrap_err();
...
*/
doomed_make_proposal_result.unwrap(); // TODO(NNS1-2982): Delete this line.
``` [5](#0-4) 

Unlike `TransferSnsTreasuryFunds`, which has a **second enforcement check at execution time** (`transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err`), there is no analogous execution-time guard for `MintSnsTokens`. [6](#0-5) 

### Impact Explanation

An SNS governance participant (or coalition) controlling enough voting power to pass proposals can submit N `MintSnsTokens` proposals within a single 7-day window, each for any `amount_e8s` up to `u64::MAX`. The total minted tokens is bounded only by `u64::MAX * N` — not by the intended treasury-valuation cap (which would typically allow at most ~25% of treasury value or 300,000 XDR equivalent per 7 days). For any SNS with a treasury worth >$1M, this allows minting orders of magnitude more tokens than the protocol intends to permit.

### Likelihood Explanation

The attacker needs enough SNS voting power to pass proposals — this is the governance threshold, not a privileged/admin role. A whale neuron holder or coordinated group of token holders can exploit this without any special access. The 7-day cap is specifically designed to protect against this exact scenario (a governance majority acting against the interests of minority holders), and its absence is a direct protocol-level failure.

### Recommendation

Uncomment the real `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` (resolving TODO NNS1-2982), and add an execution-time enforcement check for `MintSnsTokens` analogous to `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err`.

### Proof of Concept

A state-machine test that:
1. Deploys an SNS with a treasury worth >$1M
2. Submits and passes `MintSnsTokens` proposal #1 for `amount_e8s = u64::MAX / 2`
3. Submits and passes `MintSnsTokens` proposal #2 for `amount_e8s = u64::MAX / 2` in the same 7-day window
4. Asserts both proposals execute successfully (confirmed by the existing commented-out test at line 966)
5. Asserts total minted tokens far exceeds the treasury-valuation-based cap

The existing integration test at `rs/sns/integration_tests/src/sns_treasury.rs:966` already demonstrates step 4 — `doomed_make_proposal_result.unwrap()` passes, confirming the second mint is not rejected. [7](#0-6)

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

**File:** rs/sns/governance/src/proposal.rs (L2600-2617)
```rust
pub(crate) fn transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err<'a>(
    transfer: &TransferSnsTreasuryFunds,
    valuation: Valuation,
    proposals: impl Iterator<Item = &'a ProposalData>,
    now_timestamp_seconds: u64,
) -> Result<(), GovernanceError> {
    let allowance_tokens = transfer_sns_treasury_funds_7_day_total_upper_bound_tokens(valuation)
        .map_err(|err| {
            // This should not be possible, because valuation was already used the same way during
            // proposal submission/creation/validation.
            GovernanceError::new_with_message(
                ErrorType::InconsistentInternalData,
                format!(
                    "Unable to determined upper bound on the amount of \
                     TransferSnsTreasuryFunds proposals: {err:?}\nvaluation:{valuation:?}",
                ),
            )
        })?;
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L14-18)
```rust
pub fn mint_sns_tokens_7_day_total_upper_bound_tokens(
    valuation: Valuation,
) -> Result<Decimal, ProposalsAmountTotalLimitError> {
    ProposalsAmountTotalUpperBound::in_tokens(valuation)
}
```

**File:** rs/sns/integration_tests/src/sns_treasury.rs (L930-966)
```rust
    // Whale tries again, but this time, it doesn't work, because of minting limits.
    let doomed_make_proposal_result = sns_make_proposal(
        &state_machine,
        governance_canister_id,
        *WHALE,
        whale_neuron_id,
        Proposal {
            title: "Second Mint".to_string(),
            ..proposal
        },
    );

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
