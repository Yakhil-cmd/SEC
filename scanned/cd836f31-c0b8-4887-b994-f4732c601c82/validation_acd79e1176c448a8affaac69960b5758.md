### Title
Missing Execution-Time 7-Day Amount Check for `MintSnsTokens` Proposals — (`rs/sns/governance/src/governance.rs`)

### Summary

`perform_transfer_sns_treasury_funds` enforces a critical execution-time spending-limit check (`transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err`) that prevents the 7-day total upper bound from being exceeded when multiple proposals are executed in sequence. The analogous `perform_mint_sns_tokens` path receives no valuation and performs no equivalent execution-time check, even though `MintSnsTokens` is governed by the same 7-day total upper-bound regime.

### Finding Description

In `rs/sns/governance/src/governance.rs`, `perform_action` dispatches adopted proposals to type-specific handlers:

```rust
Action::TransferSnsTreasuryFunds(transfer) => {
    let valuation =
        get_action_auxiliary(&self.proto.proposals, ProposalId { id: proposal_id })
            .and_then(|action_auxiliary| {
                action_auxiliary.unwrap_transfer_sns_treasury_funds_or_err()
            });
    self.perform_transfer_sns_treasury_funds(proposal_id, valuation, &transfer)
        .await
}
Action::MintSnsTokens(mint) => self.perform_mint_sns_tokens(mint).await,
``` [1](#0-0) 

For `TransferSnsTreasuryFunds`, the submission-time `Valuation` is retrieved from `action_auxiliary` and forwarded to `perform_transfer_sns_treasury_funds`, which immediately calls:

```rust
transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err(
    transfer,
    valuation?,
    self.proto.proposals.values(),
    self.env.now(),
)?;
``` [2](#0-1) 

This function re-tallies all executed `TransferSnsTreasuryFunds` proposals from the past 7 days and rejects execution if the running total would exceed the valuation-based cap: [3](#0-2) 

For `MintSnsTokens`, `perform_mint_sns_tokens` receives only the `MintSnsTokens` struct — no `proposal_id`, no `Valuation`, no `action_auxiliary` lookup. The function signature makes it structurally impossible to perform the same execution-time re-check. The only guard is the submission-time check inside `validate_and_render_action`, which runs once when the proposal is created and cannot account for other proposals that are adopted but not yet executed.

Both action types share the same 7-day upper-bound infrastructure (`mint_sns_tokens_7_day_total_upper_bound_tokens` mirrors `transfer_sns_treasury_funds_7_day_total_upper_bound_tokens`): [4](#0-3) 

### Impact Explanation

An SNS governance process can inadvertently (or deliberately, with a sufficient but not necessarily malicious quorum) execute multiple `MintSnsTokens` proposals whose combined minted amount exceeds the 7-day cap:

1. Proposal A mints amount X — passes submission-time check (past-7-day total = 0, cap = L).
2. Proposal B mints amount Y — passes submission-time check (past-7-day total still = 0, A not yet executed).
3. Both proposals are adopted.
4. Proposal A executes: total minted = X.
5. Proposal B executes: total minted = X + Y > L — **no execution-time guard fires**.

The result is unbounded token inflation beyond the protocol-intended cap, diluting all existing SNS token holders and undermining the economic safety guarantees the 7-day limit is designed to provide.

### Likelihood Explanation

Moderate. No single malicious actor is required. The scenario arises naturally when multiple neuron holders independently submit `MintSnsTokens` proposals within the same 7-day window, each passing the per-proposal submission check, and the proposals are later executed in sequence. The gap between proposal adoption and execution (which can span hours to days) makes this a realistic race condition in any active SNS.

### Recommendation

Mirror the `TransferSnsTreasuryFunds` pattern for `MintSnsTokens`:

1. Store a `MintSnsTokens`-specific `Valuation` in `ActionAuxiliary` at proposal-submission time (analogous to `ActionAuxiliary::TransferSnsTreasuryFunds`).
2. In `perform_action`, retrieve that auxiliary valuation via `get_action_auxiliary` and pass it to `perform_mint_sns_tokens`.
3. Inside `perform_mint_sns_tokens`, call an execution-time guard (analogous to `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err`) that re-tallies all executed `MintSnsTokens` proposals from the past 7 days and rejects execution if the running total would exceed the cap.

### Proof of Concept

```
// Submission-time state: 0 tokens minted in past 7 days, cap = L

// Neuron A submits MintSnsTokens(amount = L * 0.6)  → passes (0 + 0.6L ≤ L)
// Neuron B submits MintSnsTokens(amount = L * 0.6)  → passes (0 + 0.6L ≤ L, A not executed)

// Both proposals adopted by governance vote.

// Execution:
//   Proposal A executes → minted = 0.6L  (no execution-time check)
//   Proposal B executes → minted = 1.2L  (no execution-time check — exceeds cap by 20%)
```

The `TransferSnsTreasuryFunds` path would have rejected Proposal B's execution at step 2 via `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err`. The `MintSnsTokens` path has no equivalent gate. [1](#0-0) [5](#0-4) [6](#0-5)

### Citations

**File:** rs/sns/governance/src/governance.rs (L2203-2212)
```rust
            Action::TransferSnsTreasuryFunds(transfer) => {
                let valuation =
                    get_action_auxiliary(&self.proto.proposals, ProposalId { id: proposal_id })
                        .and_then(|action_auxiliary| {
                            action_auxiliary.unwrap_transfer_sns_treasury_funds_or_err()
                        });
                self.perform_transfer_sns_treasury_funds(proposal_id, valuation, &transfer)
                    .await
            }
            Action::MintSnsTokens(mint) => self.perform_mint_sns_tokens(mint).await,
```

**File:** rs/sns/governance/src/governance.rs (L2980-3005)
```rust
    async fn perform_transfer_sns_treasury_funds(
        &mut self,
        proposal_id: u64, // This is just to control concurrency.
        valuation: Result<Valuation, GovernanceError>,
        transfer: &TransferSnsTreasuryFunds,
    ) -> Result<(), GovernanceError> {
        // Only execute one proposal of this type at a time.
        thread_local! {
            static IN_PROGRESS_PROPOSAL_ID: RefCell<Option<u64>> = const { RefCell::new(None) };
        }
        let release_on_drop = acquire(&IN_PROGRESS_PROPOSAL_ID, proposal_id);
        if let Err(already_in_progress_proposal_id) = release_on_drop {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "Another TransferSnsTreasuryFunds proposal (ID = {already_in_progress_proposal_id}) is already in progress.",
                ),
            ));
        }

        transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err(
            transfer,
            valuation?,
            self.proto.proposals.values(),
            self.env.now(),
        )?;
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

**File:** rs/sns/governance/proposals_amount_total_limit/src/tests.rs (L57-70)
```rust
    let observed_treasury_upper_bound_tokens =
        transfer_sns_treasury_funds_7_day_total_upper_bound_tokens(valuation).unwrap();
    let observed_minting_upper_bound_tokens =
        mint_sns_tokens_7_day_total_upper_bound_tokens(valuation).unwrap();

    assert_eq!(
        observed_treasury_upper_bound_tokens,
        Decimal::from(50_000 / 4),
    );
    assert_eq!(
        observed_minting_upper_bound_tokens,
        Decimal::from(50_000 / 4),
    );
}
```
