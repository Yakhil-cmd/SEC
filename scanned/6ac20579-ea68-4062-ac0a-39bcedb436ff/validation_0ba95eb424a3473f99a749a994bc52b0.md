### Title
SNS 7-Day Treasury Transfer Cap Ignores Ledger Transfer Fee Outflow - (File: `rs/sns/governance/src/proposal.rs`)

### Summary

The SNS governance 7-day treasury transfer cap is enforced using only `amount_e8s` (the net recipient amount), but the actual treasury outflow per executed `TransferSnsTreasuryFunds` proposal is `amount_e8s + fee_e8s`. The ledger deducts both from the treasury account, so the cap systematically understates usage by one transfer fee per executed proposal.

### Finding Description

`perform_transfer_sns_treasury_funds` in `rs/sns/governance/src/governance.rs` calls the ledger with both `amount_e8s` and a fee:

```rust
// ICP treasury path
self.nns_ledger.transfer_funds(
    transfer.amount_e8s,
    NNS_DEFAULT_TRANSFER_FEE.get_e8s(),   // 10_000 e8s
    self.sns_treasury_icp_subaccount(),
    to,
    transfer.memo.unwrap_or(0),
)
``` [1](#0-0) 

The ICP ledger deducts `amount_e8s + fee_e8s` from the source (treasury) account. This is confirmed by every ledger implementation in the codebase:

```rust
let requested_e8s = amount_e8s + fee_e8s;
// ...
*from_e8s -= requested_e8s;
``` [2](#0-1) [3](#0-2) 

However, `total_treasury_transfer_amount_tokens` — the function that computes how much has been spent against the 7-day cap — only returns `transfer.amount_e8s`:

```rust
Some(transfer.amount_e8s)   // fee_e8s is never added
``` [4](#0-3) 

This function is called at both proposal submission time and execution time: [5](#0-4) [6](#0-5) 

The same omission applies to the SNS token treasury path, where `transaction_fee_e8s` is passed to the ledger but not counted against the cap: [7](#0-6) 

### Impact Explanation

The 7-day treasury transfer cap is the primary on-chain protection against rapid SNS treasury depletion via governance proposals. Each executed `TransferSnsTreasuryFunds` proposal drains `amount_e8s + fee_e8s` from the treasury but only `amount_e8s` is recorded against the cap. Over `N` proposals within the 7-day window, the treasury is drained by an extra `N × fee_e8s` beyond the intended cap. For ICP, `NNS_DEFAULT_TRANSFER_FEE = 10_000 e8s = 0.0001 ICP` per proposal. For SNS tokens, the fee varies per SNS configuration. The discrepancy is small per transaction but is a real accounting mismatch: the cap does not accurately reflect actual reserve outflow, which is the exact invariant it is designed to enforce. [8](#0-7) 

### Likelihood Explanation

This is triggered by every successfully executed `TransferSnsTreasuryFunds` proposal. Any SNS token holder with sufficient voting power to pass such a proposal can trigger it. The SNS governance system is explicitly designed to allow this action. No privileged key or admin access is required beyond normal SNS governance participation. The discrepancy is deterministic and occurs on every execution. [9](#0-8) 

### Recommendation

In `total_treasury_transfer_amount_tokens`, count the full treasury outflow per proposal rather than only `amount_e8s`. For ICP treasury proposals, add `NNS_DEFAULT_TRANSFER_FEE.get_e8s()` to the recorded amount. For SNS token treasury proposals, add the SNS ledger's `transaction_fee_e8s`. Alternatively, document explicitly that the cap is defined as the net recipient amount (excluding fees), and adjust the cap threshold accordingly so operators and risk models match the on-chain behavior. [10](#0-9) 

### Proof of Concept

1. An SNS has a treasury of 1,000 ICP. The 7-day cap is 25% = 250 ICP (medium treasury regime).
2. A whale submits and passes a `TransferSnsTreasuryFunds` proposal with `amount_e8s = 250 * E8 - NNS_DEFAULT_TRANSFER_FEE.get_e8s()` (the maximum allowed, just under the cap).
3. The proposal executes: `transfer_funds(amount_e8s=24_999_990_000, fee_e8s=10_000, ...)` is called. The treasury loses `25_000_000_000 e8s = 250 ICP` exactly.
4. `total_treasury_transfer_amount_tokens` records only `24_999_990_000 e8s` against the cap.
5. The cap shows `249.9999 ICP` used out of `250 ICP` allowed — `10_000 e8s` of headroom remains.
6. A second proposal with `amount_e8s = 10_000` (the minimum, equal to the fee) can now be submitted and executed, draining an additional `20_000 e8s` from the treasury (amount + fee), even though the cap should have been exhausted.
7. Total actual treasury drain: `250 ICP + 0.0002 ICP = 250.0002 ICP`, exceeding the intended 250 ICP cap. [11](#0-10) [12](#0-11)

### Citations

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

**File:** rs/sns/governance/src/governance.rs (L3018-3034)
```rust
            TransferFrom::IcpTreasury => self
                .nns_ledger
                .transfer_funds(
                    transfer.amount_e8s,
                    NNS_DEFAULT_TRANSFER_FEE.get_e8s(),
                    self.sns_treasury_icp_subaccount(),
                    to,
                    transfer.memo.unwrap_or(0),
                )
                .await
                .map(|_| ())
                .map_err(|e| {
                    GovernanceError::new_with_message(
                        ErrorType::External,
                        format!("Error making ICP treasury transfer: {e}"),
                    )
                }),
```

**File:** rs/sns/governance/src/governance.rs (L3035-3054)
```rust
            TransferFrom::SnsTokenTreasury => {
                let transaction_fee_e8s = self.transaction_fee_e8s_or_panic();

                self.ledger
                    .transfer_funds(
                        transfer.amount_e8s,
                        transaction_fee_e8s,
                        self.sns_treasury_sns_token_subaccount(),
                        to,
                        transfer.memo.unwrap_or(0),
                    )
                    .await
                    .map(|_| ())
                    .map_err(|e| {
                        GovernanceError::new_with_message(
                            ErrorType::External,
                            format!("Error making SNS Token treasury transfer: {e}"),
                        )
                    })
            }
```

**File:** rs/nns/governance/tests/fake.rs (L342-358)
```rust
        let requested_e8s = amount_e8s + fee_e8s;

        if !is_minting_operation {
            if *from_e8s < requested_e8s {
                tla_log_response!(
                    Destination::new("ledger"),
                    tla::TlaValue::Variant {
                        tag: "Fail".to_string(),
                        value: Box::new(tla::TlaValue::Constant("UNIT".to_string()))
                    }
                );
                return Err(NervousSystemError::new_with_message(format!(
                    "Insufficient funds. Available {} requested {}",
                    *from_e8s, requested_e8s
                )));
            }
            *from_e8s -= requested_e8s;
```

**File:** rs/sns/governance/tests/fixtures/mod.rs (L113-122)
```rust
            let requested_e8s = amount_e8s + fee_e8s;

            if *from_e8s < requested_e8s {
                return Err(NervousSystemError::new_with_message(format!(
                    "Insufficient funds. Available {} requested {}",
                    *from_e8s, requested_e8s
                )));
            }

            *from_e8s -= requested_e8s;
```

**File:** rs/sns/governance/src/proposal.rs (L851-861)
```rust
    fn recent_amount_total_tokens<'a>(
        &self,
        proposals: impl Iterator<Item = &'a ProposalData>,
        now_timestamp_seconds: u64,
    ) -> Result<Decimal, String> {
        total_treasury_transfer_amount_tokens(
            proposals,
            self.from_treasury(),
            now_timestamp_seconds - 7 * ONE_DAY_SECONDS,
        )
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

**File:** rs/sns/governance/src/proposal.rs (L2674-2703)
```rust
fn total_treasury_transfer_amount_tokens<'a>(
    proposals: impl Iterator<Item = &'a ProposalData>,
    filter_from_treasury: TransferFrom,
    min_executed_timestamp_seconds: u64,
) -> Result<Decimal, String> {
    let filter_proposal_action_amount_e8s = |action: &Action| {
        let transfer = match action {
            Action::TransferSnsTreasuryFunds(ok) => ok,
            // Skip other types of proposals.
            _ => return None,
        };

        let is_proposal_token_relevant =
            // Very confusingly, the from_treasury field specifies which token
            // the proposal is about.
            TransferFrom::try_from(transfer.from_treasury) == Ok(filter_from_treasury);
        if !is_proposal_token_relevant {
            return None;
        }

        Some(transfer.amount_e8s)
    };

    total_proposal_amounts_tokens(
        proposals,
        &format!("{filter_from_treasury:?} transfer"),
        filter_proposal_action_amount_e8s,
        min_executed_timestamp_seconds,
    )
}
```
