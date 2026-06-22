### Title
`MintSnsTokens` 7-Day Minting Cap Not Enforced — (`rs/sns/governance/src/proposal.rs`)

### Summary

The SNS governance canister defines a 7-day rolling cap on `MintSnsTokens` proposals (mirroring the cap already enforced for `TransferSnsTreasuryFunds`), but the cap enforcement is intentionally disabled via a `TODO` comment. The stub replacement returns `Decimal::MAX`, making the check a no-op. No execution-time re-check exists for `MintSnsTokens` either. Any SNS governance majority can therefore pass an unlimited number of `MintSnsTokens` proposals within a 7-day window, inflating the SNS token supply without bound.

### Finding Description

The `TokenProposalAction` trait in `rs/sns/governance/src/proposal.rs` defines `recent_amount_total_upper_bound_tokens` as the protocol-level cap on how many tokens may be minted or transferred within a rolling 7-day window.

For `TransferSnsTreasuryFunds`, the cap is fully enforced:

- At **proposal submission** time via `treasury_valuation_if_proposal_amount_is_small_enough_or_err` (which calls `recent_amount_total_upper_bound_tokens`).
- At **execution** time via `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err`. [1](#0-0) [2](#0-1) 

For `MintSnsTokens`, the correct implementation of `recent_amount_total_upper_bound_tokens` is **commented out** with `TODO(NNS1-2982)`:

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
``` [3](#0-2) 

`Decimal::MAX` as the upper bound means the check at line 805 (`if proposal_amount_tokens > allowance_remainder_tokens`) can never trigger, regardless of how many tokens have already been minted in the past 7 days. [4](#0-3) 

Furthermore, `perform_mint_sns_tokens` — the execution-time handler — contains no cap re-check at all, unlike its `TransferSnsTreasuryFunds` counterpart: [5](#0-4) 

The cap function itself (`mint_sns_tokens_7_day_total_upper_bound_tokens`) is fully implemented and correct in `rs/sns/governance/proposals_amount_total_limit/src/lib.rs` — it simply is never called for `MintSnsTokens`. [6](#0-5) 

### Impact Explanation

An SNS governance majority can submit and pass multiple `MintSnsTokens` proposals within a single 7-day window, each minting an arbitrary amount of SNS tokens to any target account. The intended rate-limiting protection — which caps minting to a fraction of the treasury value per 7-day window — is entirely absent. This allows unbounded SNS token inflation within any 7-day period, directly harming token holders through dilution and undermining the economic model of the SNS.

### Likelihood Explanation

Any SNS neuron holder (or coordinated group) with sufficient voting power to pass proposals can exploit this. The integration test `sns_can_mint_funds_via_proposals` explicitly confirms the bypass: the second `MintSnsTokens` proposal within the same 7-day window succeeds where it should fail, and the assertion that was supposed to verify rejection is commented out with the same TODO. [7](#0-6) 

A single whale neuron holder with a majority stake — a realistic scenario in many SNS deployments — can exploit this unilaterally.

### Recommendation

Uncomment the correct `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` (remove the `TODO(NNS1-2982): Uncomment` block and delete the stub) in `rs/sns/governance/src/proposal.rs`: [3](#0-2) 

Additionally, add an execution-time cap re-check in `perform_mint_sns_tokens` analogous to `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err`, to guard against race conditions between concurrent proposals. [8](#0-7) 

### Proof of Concept

1. Deploy an SNS with a whale neuron holding majority voting power.
2. Submit `MintSnsTokens` proposal #1 for an amount equal to the full 7-day cap (e.g., 25% of treasury for a medium-sized treasury). Let it pass and execute.
3. Immediately submit `MintSnsTokens` proposal #2 for the same amount. Under correct enforcement this should be rejected at submission time with "Amount is too large." Instead, it passes validation because `recent_amount_total_upper_bound_tokens` returns `Decimal::MAX`.
4. Proposal #2 executes successfully, minting tokens beyond the intended 7-day cap.

The integration test `sns_can_mint_funds_via_proposals` in `rs/sns/integration_tests/src/sns_treasury.rs` already encodes exactly this scenario and confirms the bypass at line 966 (`doomed_make_proposal_result.unwrap()`). [9](#0-8)

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

**File:** rs/sns/governance/src/proposal.rs (L2600-2658)
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

    // The total calculated here _could_ be different from what was calculated at proposal
    // submission/creation time. A difference would result from the execution of (another)
    // TransferSnsTreasuryFunds proposal between now and then.
    let spent_tokens = total_treasury_transfer_amount_tokens(
        proposals,
        transfer.from_treasury(),
        now_timestamp_seconds - 7 * ONE_DAY_SECONDS,
    )
    .map_err(|message| {
        GovernanceError::new_with_message(ErrorType::InconsistentInternalData, message)
    })?;

    let remainder_tokens = allowance_tokens - spent_tokens;
    let transfer_amount_tokens = denominations_to_tokens(transfer.amount_e8s, E8)
        // This Err cannot be provoked, because we are dividing a u64 (amount_e8s) by a positive
        // integer (E8).
        .ok_or_else(|| {
            GovernanceError::new_with_message(
                ErrorType::UnreachableCode,
                format!(
                    "Unable to convert proposals amount {} e8s to tokens.",
                    transfer.amount_e8s,
                ),
            )
        })?;
    if transfer_amount_tokens > remainder_tokens {
        return Err(GovernanceError::new_with_message(
            ErrorType::PreconditionFailed,
            format!(
                "Executing this proposal is not allowed at this time, because doing \
                 so would cause the 7 day upper bound of {allowance_tokens} tokens to be exceeded. \
                 Maybe, try again later? The total amount transferred in the past \
                 7 days stands at {spent_tokens} tokens, and the amount in this proposal is {transfer_amount_tokens} \
                 tokens. The upper bound is based on treasury valuation factors at \
                 the time of proposal submission: {valuation:?}",
            ),
        ));
    }

    Ok(())
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
