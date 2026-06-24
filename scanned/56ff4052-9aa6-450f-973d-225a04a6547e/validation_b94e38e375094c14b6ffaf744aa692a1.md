### Title
Missing Balance Re-Validation at Execution Time in SNS Treasury Manager Deposit - (`rs/sns/governance/src/extensions.rs`)

### Summary

The SNS governance `validate_deposit_operation_impl` function enforces a 50%-of-treasury-balance safety limit when a `TreasuryManagerDeposit` or `RegisterExtension` proposal is submitted. However, this check is performed only at proposal-submission/validation time and is **never re-applied at execution time**. Because SNS proposals have a voting window between submission and execution, multiple deposit proposals submitted simultaneously can each independently pass the 50% check, then execute sequentially and collectively drain far more than 50% of the treasury — violating the invariant the check was designed to enforce.

---

### Finding Description

`validate_deposit_operation_impl` is the single function that enforces the treasury-balance safety invariant: [1](#0-0) 

It is called from two proposal-validation paths:

- `validate_treasury_manager_init` (for `RegisterExtension` proposals): [2](#0-1) 

- `validate_deposit_operation` (for `ExecuteExtensionOperation` deposit proposals): [3](#0-2) 

Both are invoked from `validate_execute_extension_operation`, which runs **at proposal submission time**: [4](#0-3) 

At execution time, `ValidatedExecuteExtensionOperation::execute` dispatches to `execute_treasury_manager_deposit`: [5](#0-4) 

`execute_treasury_manager_deposit` immediately calls `approve_treasury_manager` with the amounts that were validated at submission time, **without re-checking the 50% limit against the current treasury balance**: [6](#0-5) 

By contrast, `validate_withdraw_operation` — the sibling path — takes the `_governance` parameter but performs no balance check at all: [7](#0-6) 

This mirrors the external report's pattern exactly: `_checkBalances()` is called in `_swap()` and `_lpTokenSpecified()` but omitted from `_reserveTokenSpecified()`, allowing deposit/withdraw paths to break the invariant.

---

### Impact Explanation

**Scenario:**

1. SNS treasury holds 100 SNS tokens.
2. Proposal A is submitted: deposit 50 SNS (50% of 100) → passes the 50% check at submission time.
3. Proposal B is submitted concurrently: deposit 50 SNS (50% of 100) → also passes the 50% check at submission time.
4. Both proposals pass the voting period.
5. Proposal A executes: treasury now holds 50 SNS; `icrc2_approve` for 50 SNS succeeds.
6. Proposal B executes: treasury still holds 50 SNS; `icrc2_approve` for 50 SNS succeeds — this is **100% of the remaining balance**, directly violating the 50% invariant.

The treasury manager now controls 100% of the SNS treasury, even though the safety limit was intended to cap any single operation at 50%. The ledger permits the approval because the balance is sufficient; there is no on-chain guard that enforces the 50% rule at execution time.

The same scenario applies to ICP treasury funds via the `icp_balance` branch of `validate_deposit_operation_impl`. [8](#0-7) 

---

### Likelihood Explanation

The attack does not require a malicious governance majority. It requires only that two deposit proposals are submitted during overlapping voting windows — a routine occurrence in any active SNS. The proposals can be submitted by different, independently acting token holders, each acting in good faith. No privileged access, key compromise, or subnet-majority corruption is needed. The entry path is a standard SNS governance ingress call (`submit_proposal`), reachable by any SNS token holder with a staked neuron.

---

### Recommendation

Re-validate the 50% balance limit inside `execute_treasury_manager_deposit` immediately before calling `approve_treasury_manager`, using the **current** treasury balance at execution time — analogous to the recommended fix in the external report of adding `_checkBalances(...)` after the state-modifying lines in `_reserveTokenSpecified()`.

```rust
// In execute_treasury_manager_deposit, before approve_treasury_manager:
let current_sns_balance = governance.ledger.account_balance(...).await?;
let current_icp_balance = governance.nns_ledger.account_balance(...).await?;
if Tokens::from_e8s(treasury_allocation_sns_e8s) > current_sns_balance.checked_div(2).unwrap() {
    return Err(...); // 50% limit exceeded at execution time
}
if Tokens::from_e8s(treasury_allocation_icp_e8s) > current_icp_balance.checked_div(2).unwrap() {
    return Err(...);
}
``` [9](#0-8) 

---

### Proof of Concept

The existing unit test `test_validate_deposit_operation_treasury_balance_limits` confirms the 50% check works at validation time: [10](#0-9) 

A proof-of-concept would submit two `ExecuteExtensionOperation` deposit proposals each requesting 50% of the treasury balance, advance the state machine through both voting periods, execute both proposals in sequence, and assert that the treasury balance after both executions is 0 — demonstrating that 100% of the treasury was transferred despite the 50% per-operation limit.

### Citations

**File:** rs/sns/governance/src/extensions.rs (L250-261)
```rust
/// Validates treasury manager init arguments
fn validate_treasury_manager_init(
    governance: &Governance,
    init: ExtensionInit,
) -> BoxFuture<'_, Result<ValidatedExtensionInit, String>> {
    Box::pin(async move {
        let ExtensionInit { value } = init;
        validate_deposit_operation_impl(governance, value)
            .await
            .map(ValidatedExtensionInit::TreasuryManager)
    })
}
```

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

**File:** rs/sns/governance/src/extensions.rs (L384-394)
```rust
fn validate_deposit_operation(
    governance: &Governance,
    arg: ExtensionOperationArg,
) -> BoxFuture<'_, Result<ValidatedOperationArg, String>> {
    Box::pin(async move {
        let ExtensionOperationArg { value } = arg;
        validate_deposit_operation_impl(governance, value)
            .await
            .map(ValidatedOperationArg::TreasuryManagerDeposit)
    })
}
```

**File:** rs/sns/governance/src/extensions.rs (L396-407)
```rust
/// Validates withdraw operation arguments (currently requires empty arguments)
fn validate_withdraw_operation(
    _governance: &Governance,
    arg: ExtensionOperationArg,
) -> BoxFuture<'_, Result<ValidatedOperationArg, String>> {
    Box::pin(async move {
        let ExtensionOperationArg { value } = arg;

        ValidatedWithdrawOperationArg::try_from(value)
            .map(ValidatedOperationArg::TreasuryManagerWithdraw)
    })
}
```

**File:** rs/sns/governance/src/extensions.rs (L612-615)
```rust
        match arg {
            ValidatedOperationArg::TreasuryManagerDeposit(arg) => {
                execute_treasury_manager_deposit(governance, extension_canister_id, arg).await
            }
```

**File:** rs/sns/governance/src/extensions.rs (L1526-1536)
```rust
    let validated_arg = operation_spec
        .validate_operation_arg(governance, operation_arg)
        .await
        .map_err(|err| {
            GovernanceError::new_with_message(
                ErrorType::InvalidProposal,
                format!(
                    "Extension canister {extension_canister_id} operation {operation_name} validation failed: {err}"
                ),
            )
        })?;
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

**File:** rs/sns/governance/src/extensions.rs (L2661-2720)
```rust
    #[tokio::test]
    async fn test_validate_deposit_operation_treasury_balance_limits() {
        // Test parameters: (label, sns_balance, icp_balance, sns_request, icp_request, expected_result)
        #[allow(clippy::type_complexity)]
        let test_cases: Vec<(&'static str, u64, u64, u64, u64, Result<(), &'static str>)> = vec![
            (
                "Positive: exactly 50%",
                100_000_000,
                200_000_000,
                50_000_000,
                100_000_000,
                Ok(()),
            ),
            (
                "Positive: below 50%",
                100_000_000,
                200_000_000,
                30_000_000,
                60_000_000,
                Ok(()),
            ),
            (
                "Positive: zero amounts",
                100_000_000,
                200_000_000,
                0,
                0,
                Ok(()),
            ),
            (
                "Negative: SNS exceeds 50%",
                100_000_000,
                200_000_000,
                51_000_000,
                50_000_000,
                Err(
                    "SNS treasury deposit request of 0.51000000 Token exceeds 50% of current SNS Token balance",
                ),
            ),
            (
                "Negative: ICP exceeds 50%",
                100_000_000,
                200_000_000,
                40_000_000,
                101_000_000,
                Err(
                    "ICP treasury deposit request of 1.01000000 Token exceeds 50% of current ICP balance",
                ),
            ),
            (
                "Negative: both exceed 50% (SNS checked first)",
                100_000_000,
                200_000_000,
                60_000_000,
                120_000_000,
                Err(
                    "SNS treasury deposit request of 0.60000000 Token exceeds 50% of current SNS Token balance",
                ),
            ),
        ];
```
