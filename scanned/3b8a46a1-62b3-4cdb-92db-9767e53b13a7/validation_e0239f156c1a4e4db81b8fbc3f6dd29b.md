### Title
SNS Treasury Manager Deposit Proposal Lacks Slippage Protection — Fixed Asset Amounts Become Stale Between Proposal Approval and Execution - (File: rs/sns/governance/src/extensions.rs)

### Summary
The SNS governance `ExecuteExtensionOperation` deposit proposal encodes fixed asset amounts (`treasury_allocation_sns_e8s`, `treasury_allocation_icp_e8s`) at proposal-submission time. Because the SNS governance voting period is typically four days, the DEX pool price ratio can shift arbitrarily before execution. The governance layer enforces no slippage bound, so the SNS treasury can be sandwiched: an unprivileged actor trades on the DEX to move the pool ratio, the proposal executes at the new ratio, and the SNS treasury receives fewer LP tokens than the voters approved.

### Finding Description

**Proposal submission / validation path**

`validate_and_render_execute_extension_operation` → `validate_execute_extension_operation` → `validate_deposit_operation_impl` runs at proposal-submission time. The only financial guard it applies is a 50 % balance cap:

```rust
if sns_requested > sns_balance.checked_div(2).unwrap() { … }
if icp_requested > icp_balance.checked_div(2).unwrap() { … }
```

The amounts are then frozen inside `ValidatedDepositOperationArg { treasury_allocation_sns_e8s, treasury_allocation_icp_e8s, original }` and stored with the proposal. No expected pool ratio, no minimum LP-token output, and no slippage tolerance are recorded.

**Execution path (days later)**

`perform_execute_extension_operation` re-runs `validate_execute_extension_operation` (50 % check only) and then calls `execute_treasury_manager_deposit`, which:

1. Calls `approve_treasury_manager` — issues two ICRC-2 approvals for the **original fixed amounts** to the treasury manager canister.
2. Calls `deposit` on the treasury manager with the same fixed allowances.

The DEX pool ratio at execution time is never compared against the ratio at proposal-submission time. Any ratio drift — including drift deliberately induced by a sandwich attacker — is silently accepted.

**Acknowledged but unmitigated**

The `treasury_manager.did` file explicitly flags this as a "Known Security Risk":

> Some liquidity pools do not implement slippage protection for deposits. In other words, the price ratio at the time of execution may differ from the ratio at the time the proposal was approved.

The proposal-rendering code in `validate_and_render_register_extension` also warns voters. However, neither the governance canister nor the `execute_treasury_manager_deposit` function enforces any slippage bound at execution time. The mitigation is entirely delegated to the treasury manager implementation (returning excess tokens), which is a trust assumption, not a protocol guarantee.

### Impact Explanation
An unprivileged DEX trader can front-run or sandwich the deposit proposal execution window. The SNS treasury approves the fixed amounts, the treasury manager pulls them, and the DEX mints fewer LP tokens than the governance voters expected. The SNS DAO suffers a quantifiable loss of LP-token value proportional to the induced price impact. Because the ICRC-2 allowance is set to the full fixed amounts, the treasury manager can pull all approved tokens even when the pool would only absorb a fraction at the original ratio; any unabsorbed tokens depend on the treasury manager's refund logic, which is not enforced on-chain by the governance canister.

### Likelihood Explanation
The attack requires no privileged access. Any market participant can trade on the DEX. The four-day governance voting period provides a large window in which to move the pool ratio. The deposit proposal's execution time is publicly observable on-chain, making targeted sandwiching straightforward. The `treasury_manager.did` itself acknowledges the risk exists in production.

### Recommendation
Add a `min_lp_tokens_out` (or equivalent slippage-tolerance) field to the `ExecuteExtensionOperation` deposit argument. At execution time, `execute_treasury_manager_deposit` should pass this bound to the treasury manager and revert if the actual LP tokens received fall below it. Alternatively, enforce a maximum time-to-live between proposal adoption and execution for deposit proposals, and re-validate the pool ratio at execution time against a freshly fetched oracle price.

### Proof of Concept

1. SNS governance submits a deposit proposal encoding `treasury_allocation_sns_e8s = X` and `treasury_allocation_icp_e8s = Y`, calibrated to the current DEX pool ratio R.
2. `validate_deposit_operation_impl` passes (both amounts ≤ 50 % of treasury balances). [1](#0-0) 
3. The proposal enters the four-day voting period and is adopted.
4. Before execution, an attacker trades on the DEX, shifting the pool ratio from R to R′ (e.g., doubling the price of SNS relative to ICP).
5. `perform_execute_extension_operation` re-validates (50 % check only, passes) and calls `execute_treasury_manager_deposit`. [2](#0-1) 
6. `approve_treasury_manager` issues ICRC-2 approvals for the original fixed amounts X and Y. [3](#0-2) 
7. `deposit` is called on the treasury manager with the same fixed allowances. [4](#0-3) 
8. The DEX mints LP tokens at ratio R′. The SNS treasury receives significantly fewer LP tokens than the governance voters approved, with no on-chain revert or slippage guard. [5](#0-4)

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

**File:** rs/sns/governance/src/extensions.rs (L777-831)
```rust
    async fn approve_treasury_manager(
        &self,
        treasury_manager_canister_id: CanisterId,
        sns_amount_e8s: u64,
        icp_amount_e8s: u64,
    ) -> Result<(), GovernanceError> {
        let to = Account {
            owner: treasury_manager_canister_id.get().0,
            subaccount: None,
        };

        let expiry_time_sec = self.env.now().saturating_add(ONE_HOUR_SECONDS);
        let expiry_time_nsec = expiry_time_sec.saturating_mul(NANO_SECONDS_PER_SECOND);

        // If expected_allowance is None, the ledger *blindly* overwrites any existing
        // allowance (even if non-zero). Therefore, there is no risk of double spending.

        self.ledger
            .icrc2_approve(
                to,
                sns_amount_e8s,
                Some(expiry_time_nsec),
                self.transaction_fee_e8s_or_panic(),
                self.sns_treasury_subaccount(),
                None,
            )
            .await
            .map(|_| ())
            .map_err(|e| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!("Error making SNS Token treasury transfer: {e}"),
                )
            })?;

        self.nns_ledger
            .icrc2_approve(
                to,
                icp_amount_e8s,
                Some(expiry_time_nsec),
                icp_ledger::DEFAULT_TRANSFER_FEE.get_e8s(),
                self.icp_treasury_subaccount(),
                None,
            )
            .await
            .map(|_| ())
            .map_err(|e| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!("Error making ICP Token treasury transfer: {e}"),
                )
            })?;

        Ok(())
    }
```

**File:** rs/sns/governance/src/extensions.rs (L1566-1601)
```rust
    // 1. Transfer funds from treasury to treasury manager
    governance
        .approve_treasury_manager(
            extension_canister_id,
            treasury_allocation_sns_e8s,
            treasury_allocation_icp_e8s,
        )
        .await?;

    // 2. Call deposit on treasury manager
    let balances = governance
        .env
        .call_canister(extension_canister_id, "deposit", arg_blob)
        .await
        .map_err(|(code, err)| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!(
                    "Canister method call {extension_canister_id}.deposit failed with code {code:?}: {err}"
                ),
            )
        })
        .and_then(|blob| {
            Decode!(&blob, sns_treasury_manager::TreasuryManagerResult).map_err(|err| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!("Error decoding TreasuryManager.deposit response: {err:?}"),
                )
            })
        })?
        .map_err(|err| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!("TreasuryManager.deposit failed: {err:?}"),
            )
        })?;
```

**File:** rs/sns/governance/src/governance.rs (L2558-2577)
```rust
    async fn perform_execute_extension_operation(
        &self,
        execute_extension_operation: ExecuteExtensionOperation,
    ) -> Result<(), GovernanceError> {
        // Check if SNS extensions are enabled
        if !crate::is_sns_extensions_enabled() {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "SNS extensions are not enabled",
            ));
        }

        let validated_operation =
            validate_execute_extension_operation(self, execute_extension_operation).await?;

        // Execute the validated operation
        validated_operation.execute(self).await?;

        Ok(())
    }
```

**File:** rs/sns/treasury_manager/treasury_manager.did (L35-40)
```text
// Known Security Risks:
// ====================
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.
```
