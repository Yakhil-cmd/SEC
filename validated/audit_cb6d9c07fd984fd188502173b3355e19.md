### Title
`MintSnsTokens` 7-Day Token Minting Cap Not Enforced — (`rs/sns/governance/src/proposal.rs`)

---

### Summary

The `MintSnsTokens` SNS governance proposal action has its 7-day token minting limit intentionally disabled in production code. The `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` returns `Decimal::MAX` instead of the real treasury-valuation-based cap, meaning any SNS governance majority can mint an unbounded quantity of SNS tokens within a single 7-day window, bypassing the economic safeguard that is the direct analog of the TraitForge `genMintCount` cap.

---

### Finding Description

In `rs/sns/governance/src/proposal.rs`, the `TokenProposalAction` trait is implemented for both `TransferSnsTreasuryFunds` and `MintSnsTokens`. For `TransferSnsTreasuryFunds`, `recent_amount_total_upper_bound_tokens` correctly delegates to `transfer_sns_treasury_funds_7_day_total_upper_bound_tokens`, which enforces a treasury-size-proportional cap. For `MintSnsTokens`, the real implementation is commented out and replaced with a stub that returns `Decimal::MAX`: [1](#0-0) 

The check that enforces the cap at proposal submission time is: [2](#0-1) 

Because `allowance_remainder_tokens = Decimal::MAX - spent_tokens ≈ Decimal::MAX`, the condition `proposal_amount_tokens > allowance_remainder_tokens` is never true for any realistic `MintSnsTokens` amount. The cap is completely unenforced.

There is also **no execution-time re-check** for `MintSnsTokens` analogous to `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err`: [3](#0-2) 

That execution-time guard exists only for `TransferSnsTreasuryFunds`. `MintSnsTokens` has no equivalent, so even if a submission-time check were added later, concurrent proposals could still race past it.

The `total_minting_amount_tokens` helper that would accumulate past minting is correctly implemented and is even marked `#[allow(unused)]`: [4](#0-3) 

The infrastructure to enforce the limit exists; only the upper-bound return value is wrong.

The integration test explicitly documents the broken state — the assertion that the second mint should fail is commented out, and the test instead asserts that both mints succeed: [5](#0-4) 

---

### Impact Explanation

The 7-day minting cap is the primary economic safeguard preventing SNS token supply inflation via governance. With the cap returning `Decimal::MAX`, a governance majority can pass back-to-back `MintSnsTokens` proposals minting arbitrary quantities of SNS tokens — far beyond what the treasury valuation would normally permit — within a single 7-day window. This directly breaks the token economy of any SNS project: token holders' stakes are diluted without bound, and the treasury-proportional limit (25% of medium treasury, 300 000 XDR cap for large treasury) defined in `ProposalsAmountTotalUpperBound` is rendered meaningless. [6](#0-5) 

---

### Likelihood Explanation

The entry path is the standard SNS governance proposal flow, reachable by any neuron holder who can assemble a voting majority. The 7-day limit is specifically designed to constrain what even a legitimate governance majority can do; its absence means the constraint is simply gone. No privileged key, subnet compromise, or external oracle is required — only a passing governance vote on a `MintSnsTokens` proposal, which is normal protocol operation.

---

### Recommendation

Uncomment the real `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` (the one delegating to `mint_sns_tokens_7_day_total_upper_bound_tokens`) and delete the placeholder returning `Decimal::MAX`. Additionally, add an execution-time re-check for `MintSnsTokens` analogous to `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` to guard against concurrent proposals racing past the submission-time check.

---

### Proof of Concept

1. Deploy an SNS with a treasury valued above 100 000 XDR (medium regime, 25% cap applies).
2. Submit a `MintSnsTokens` proposal for an amount exceeding 25% of the treasury token balance.
3. Pass the proposal via governance vote.
4. Observe that the proposal executes successfully — the check at `rs/sns/governance/src/proposal.rs:805` never fires because `allowance_remainder_tokens` is `Decimal::MAX`.
5. Repeat immediately with another proposal of the same size. Both execute within the same 7-day window, minting more than the intended cap in total.

The existing integration test at `rs/sns/integration_tests/src/sns_treasury.rs:966` already demonstrates this: `doomed_make_proposal_result.unwrap()` — the second mint that should be rejected succeeds. [7](#0-6)

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

**File:** rs/sns/governance/src/proposal.rs (L2600-2659)
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
}
```

**File:** rs/sns/governance/src/proposal.rs (L2707-2728)
```rust
#[allow(unused)] // TODO(NNS1-2910): Delete this.
fn total_minting_amount_tokens<'a>(
    proposals: impl Iterator<Item = &'a ProposalData>,
    min_executed_timestamp_seconds: u64,
) -> Result<Decimal, String> {
    let filter_proposal_action_amount_e8s = |action: &Action| {
        let mint = match action {
            Action::MintSnsTokens(ok) => ok,
            // Skip other types of proposals.
            _ => return None,
        };

        mint.amount_e8s
    };

    total_proposal_amounts_tokens(
        proposals,
        "MintSnsTokens",
        filter_proposal_action_amount_e8s,
        min_executed_timestamp_seconds,
    )
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

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L34-41)
```rust
impl ProposalsAmountTotalUpperBound {
    // A treasury can be small, medium, or large. These are the boundaries between those regimes.
    const MAX_SMALL_TREASURY_SIZE_XDR: Decimal = dec!(100_000);
    const MAX_MEDIUM_TREASURY_SIZE_XDR: Decimal = dec!(1_200_000);

    // No matter how large the treasury is, not more than this amount can be removed (within a 7 day
    // window).
    const MAX_XDR: Decimal = dec!(300_000);
```
