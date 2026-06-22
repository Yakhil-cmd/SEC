### Title
Treasury Manager Deposit 50% Limit Not Re-Validated at Execution Time, Enabling Concurrent Proposals to Exceed the Limit - (File: rs/sns/governance/src/extensions.rs)

### Summary
The `validate_deposit_operation_impl` function enforces a 50% treasury balance limit at proposal creation/validation time only. Because this check is not re-run at execution time, two concurrently-submitted proposals — each individually valid — can execute sequentially and together transfer more than 50% (up to 100%) of the treasury into the treasury manager canister.

### Finding Description
In `rs/sns/governance/src/extensions.rs`, `validate_deposit_operation_impl` checks that a `TreasuryManagerDeposit` request does not exceed 50% of the current SNS or ICP treasury balance:

```rust
if sns_requested > sns_balance.checked_div(2).unwrap() {
    return Err(...)
}
if icp_requested > icp_balance.checked_div(2).unwrap() {
    return Err(...)
}
``` [1](#0-0) 

This check is performed at proposal creation time (via `validate_treasury_manager_init` and `validate_deposit_operation`). However, `execute_treasury_manager_deposit` receives a pre-validated `ValidatedDepositOperationArg` and directly calls `approve_treasury_manager` with the stored amounts — it never re-invokes `validate_deposit_operation_impl`. [2](#0-1) 

This is structurally different from `TransferSnsTreasuryFunds` proposals, which explicitly re-check the 7-day spending limit at execution time via `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err`. [3](#0-2) 

The root cause is identical to the reported `getFreezableAmount` pattern: the limit is computed against the current balance at validation time, without accounting for already-committed (but not yet executed) transfers that will reduce that balance.

### Impact Explanation
Two concurrent `TreasuryManagerDeposit` proposals, each requesting exactly 50% of the treasury balance, can both pass governance validation and voting. When the first executes, the treasury balance is halved. When the second executes, it transfers 50% of the original balance from a treasury that now holds only 50% of the original — effectively transferring 100% of the remaining balance. The 50% safety limit, intended to prevent a single governance action from draining more than half the treasury into the treasury manager, is bypassed. In the general case, through N sequential proposals each requesting 50% of the current balance, the fraction of the original treasury deposited approaches 100%.

### Likelihood Explanation
Low. Exploiting this requires two concurrent governance proposals to both pass voting. However, governance voters evaluating each proposal individually may not recognize that the combined effect of two simultaneously-open proposals exceeds the 50% limit. This is a plausible scenario in active SNS governance without requiring any malicious actor — it can arise from legitimate but uncoordinated governance activity.

### Recommendation
Re-validate the 50% limit inside `execute_treasury_manager_deposit` against the live treasury balance at execution time, mirroring the pattern used for `TransferSnsTreasuryFunds` proposals. Specifically, before calling `approve_treasury_manager`, fetch the current balances and reject execution if the stored amounts now exceed 50% of the current balance.

### Proof of Concept
1. SNS treasury balance = 100 tokens.
2. Proposal A submitted: deposit 50 tokens (50% of 100) → `validate_deposit_operation_impl` passes.
3. Proposal B submitted: deposit 50 tokens (50% of 100) → `validate_deposit_operation_impl` passes.
4. Both proposals pass governance voting.
5. Proposal A executes: 50 tokens transferred to treasury manager; treasury balance = 50 tokens.
6. Proposal B executes: `execute_treasury_manager_deposit` uses the pre-validated 50-token amount without re-checking; 50 tokens transferred from a treasury of 50 tokens → treasury balance = 0.
7. Result: 100% of the original treasury deposited into the treasury manager, bypassing the 50% limit enforced at lines 308–318 of `rs/sns/governance/src/extensions.rs`. [4](#0-3)

### Citations

**File:** rs/sns/governance/src/extensions.rs (L276-321)
```rust
async fn validate_deposit_operation_impl(
    governance: &Governance,
    value: Option<Precise>,
) -> Result<ValidatedDepositOperationArg, String> {
    let structurally_valid = ValidatedDepositOperationArg::try_from(value)?;

    let sns_subaccount = governance.sns_treasury_subaccount();
    let icp_subaccount = governance.icp_treasury_subaccount();

    // Fail if either is asking for more than 50% of current balance.  The balance could have changed
    // since the proposal was created, and we don't assume that the proposal should work
    let sns_balance = governance
        .ledger
        .account_balance(Account {
            owner: governance.env.canister_id().get().0,
            subaccount: sns_subaccount,
        })
        .await
        .map_err(|e| format!("Failed to get SNS treasury balance: {e:?}"))?;
    let icp_balance = governance
        .nns_ledger
        .account_balance(Account {
            owner: governance.env.canister_id().get().0,
            subaccount: icp_subaccount,
        })
        .await
        .map_err(|e| format!("Failed to get ICP treasury balance: {e:?}"))?;

    let icp_requested = Tokens::from_e8s(structurally_valid.treasury_allocation_icp_e8s);
    let sns_requested = Tokens::from_e8s(structurally_valid.treasury_allocation_sns_e8s);

    // Unwrap is safe, only fails if divisor is zero, which we don't do.
    if sns_requested > sns_balance.checked_div(2).unwrap() {
        return Err(format!(
            "SNS treasury deposit request of {sns_requested} exceeds 50% of current SNS Token balance of {sns_balance}"
        ));
    }

    if icp_requested > icp_balance.checked_div(2).unwrap() {
        return Err(format!(
            "ICP treasury deposit request of {icp_requested} exceeds 50% of current ICP balance of {icp_balance}"
        ));
    }

    Ok(structurally_valid)
}
```

**File:** rs/sns/governance/src/extensions.rs (L1545-1573)
```rust
/// Execute a treasury manager deposit operation
async fn execute_treasury_manager_deposit(
    governance: &Governance,
    extension_canister_id: CanisterId,
    arg: ValidatedDepositOperationArg,
) -> Result<(), GovernanceError> {
    let ValidatedDepositOperationArg {
        treasury_allocation_sns_e8s,
        treasury_allocation_icp_e8s,
        original,
    } = arg;

    let context = governance.treasury_manager_deposit_context().await?;
    let arg_blob =
        construct_treasury_manager_deposit_payload(context, original).map_err(|err| {
            GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!("Failed to construct treasury manager deposit payload: {err}"),
            )
        })?;

    // 1. Transfer funds from treasury to treasury manager
    governance
        .approve_treasury_manager(
            extension_canister_id,
            treasury_allocation_sns_e8s,
            treasury_allocation_icp_e8s,
        )
        .await?;
```

**File:** rs/sns/governance/src/proposal.rs (L2600-2631)
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
```
