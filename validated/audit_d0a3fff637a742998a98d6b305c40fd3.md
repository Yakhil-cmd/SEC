### Title
Zero-Amount Treasury Deposit Proposal Bypasses Validation and Causes Fee Drain and Allowance Revocation in SNS Governance — (File: `rs/sns/governance/src/extensions.rs`)

---

### Summary

`validate_deposit_operation_impl` in SNS governance does not reject zero-amount deposit requests. A governance proposal with `treasury_allocation_sns_e8s = 0` and/or `treasury_allocation_icp_e8s = 0` passes all validation checks and, when executed, causes `approve_treasury_manager` to unconditionally call `icrc2_approve` with amount `0` on both the SNS and ICP ledgers. Per ICRC-2 semantics, an approve with amount `0` **revokes** any existing allowance and still **charges the approval fee** from the SNS treasury.

---

### Finding Description

**Step 1 — Validation allows zero amounts.**

`validate_deposit_operation_impl` only enforces that the requested amount does not exceed 50% of the current treasury balance. There is no lower-bound check: [1](#0-0) 

The test suite explicitly marks zero-amount requests as a passing case ("Positive: zero amounts"): [2](#0-1) 

**Step 2 — Execution unconditionally calls `approve_treasury_manager` with the zero amounts.**

`execute_treasury_manager_deposit` extracts `treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s` from the validated argument and passes them directly to `approve_treasury_manager` without any zero-guard: [3](#0-2) 

**Step 3 — `approve_treasury_manager` calls `icrc2_approve` unconditionally with the zero amounts.**

Both the SNS ledger and the ICP ledger receive an `icrc2_approve` call with `amount = 0`: [4](#0-3) 

**Step 4 — ICRC-2 semantics for amount = 0.**

In the ICRC-1 ledger implementation, `icrc2_approve` with `amount = 0`:
- **Charges the approval fee** regardless of the amount (fee is deducted from the treasury subaccount).
- **Removes any existing allowance** if one was previously set for the treasury manager. [5](#0-4) 

The fee is charged unconditionally because `apply_transaction` is called with `effective_fee = transfer_fee` before the allowance table is consulted: [6](#0-5) 

---

### Impact Explanation

1. **Fee drain from SNS treasury**: Every zero-amount deposit proposal that passes governance causes two approval-fee deductions (one SNS token fee, one ICP fee) from the SNS treasury subaccounts, with zero benefit.

2. **Allowance revocation**: If the treasury manager canister held a live, non-zero allowance from a prior deposit operation (e.g., it had not yet pulled all authorized funds), the zero-amount `icrc2_approve` call silently revokes that allowance. The treasury manager can no longer execute `icrc2_transfer_from` for the previously authorized amount, breaking any in-progress asset management operation.

---

### Likelihood Explanation

Any SNS neuron holder can submit an `ExecuteExtensionOperation` proposal with zero amounts. The proposal passes the on-chain validation step (`validate_execute_extension_operation`) because `validate_deposit_operation_impl` explicitly accepts zero values. If the proposal reaches execution (via normal governance majority), the side effects are triggered. This does not require a malicious majority — a well-intentioned but careless governance vote (e.g., treating a zero-amount deposit as a harmless no-op) is sufficient.

---

### Recommendation

Add a zero-amount guard in `validate_deposit_operation_impl` to reject proposals where both allocation fields are zero, or where either field is zero and an existing allowance would be revoked:

```rust
if structurally_valid.treasury_allocation_sns_e8s == 0
    && structurally_valid.treasury_allocation_icp_e8s == 0
{
    return Err("Deposit operation must specify a non-zero allocation for at least one asset".to_string());
}
```

Alternatively, add a skip-guard inside `approve_treasury_manager` to avoid calling `icrc2_approve` when the amount is zero, analogous to the fix recommended in the original report for `_depositToCoreLendingPool`.

---

### Proof of Concept

1. A neuron holder submits a governance proposal:
   ```
   ExecuteExtensionOperation {
     extension_canister_id: <registered treasury manager>,
     operation_name: "deposit",
     operation_arg: { treasury_allocation_sns_e8s: 0, treasury_allocation_icp_e8s: 0 }
   }
   ```
2. `validate_execute_extension_operation` → `validate_deposit_operation_impl` passes: `0 ≤ sns_balance / 2` and `0 ≤ icp_balance / 2` are both trivially true.
3. Governance majority votes to execute.
4. `execute_treasury_manager_deposit` is called; `approve_treasury_manager(treasury_manager, 0, 0)` is invoked.
5. `self.ledger.icrc2_approve(treasury_manager, 0, expiry, fee, sns_subaccount, None)` — SNS token approval fee is deducted from the SNS treasury; any existing SNS allowance for the treasury manager is revoked.
6. `self.nns_ledger.icrc2_approve(treasury_manager, 0, expiry, icp_fee, None, None)` — ICP approval fee is deducted from the ICP treasury; any existing ICP allowance for the treasury manager is revoked.
7. The treasury manager's `deposit` endpoint is then called with the zero-allowance context, and any subsequent `icrc2_transfer_from` it attempts will fail with `InsufficientAllowance`.

### Citations

**File:** rs/sns/governance/src/extensions.rs (L304-318)
```rust
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
```

**File:** rs/sns/governance/src/extensions.rs (L794-830)
```rust
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
```

**File:** rs/sns/governance/src/extensions.rs (L1551-1573)
```rust
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

**File:** rs/sns/governance/src/extensions.rs (L2682-2689)
```rust
            (
                "Positive: zero amounts",
                100_000_000,
                200_000_000,
                0,
                0,
                Ok(()),
            ),
```

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L293-298)
```rust
                    if amount == AD::Tokens::zero() {
                        if let Some(expires_at) = old_allowance.expires_at {
                            table.allowances_data.remove_expiry(expires_at, key.clone());
                        }
                        table.allowances_data.remove_allowance(&key);
                        return Ok(amount);
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L876-877)
```rust
        let (block_idx, _) = apply_transaction(ledger, tx, now, expected_fee_tokens)
            .map_err(convert_transfer_error)
```
