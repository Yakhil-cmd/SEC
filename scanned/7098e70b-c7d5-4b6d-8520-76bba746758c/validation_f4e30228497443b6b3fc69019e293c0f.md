### Title
SNS Treasury Deposit 50% Limit Bypassed via Multiple Successive Deposit Proposals - (`File: rs/sns/governance/src/extensions.rs`)

### Summary

The `validate_deposit_operation_impl` function in the SNS governance extension system enforces a 50% cap on each individual treasury deposit proposal, but does not account for the cumulative amount already deposited (or approved) to the treasury manager across multiple proposals. An SNS token holder with sufficient voting power can pass multiple deposit proposals in sequence, each individually below 50%, to drain the entire SNS or ICP treasury into the treasury manager canister.

### Finding Description

In `rs/sns/governance/src/extensions.rs`, the function `validate_deposit_operation_impl` (lines 276–321) is called both at proposal submission time (`validate_deposit_operation`) and at proposal execution time. It enforces a per-proposal limit: each deposit request must not exceed 50% of the current treasury balance.

```rust
if sns_requested > sns_balance.checked_div(2).unwrap() {
    return Err(format!(
        "SNS treasury deposit request of {sns_requested} exceeds 50% of current SNS Token balance of {sns_balance}"
    ));
}
```

The check reads the **current live balance** of the treasury account at the time of validation and compares it against the single proposal's requested amount. It does **not** account for:

1. Funds already approved (via `icrc2_approve`) to a treasury manager from a prior deposit proposal that has been validated but not yet fully settled.
2. Funds already transferred to the treasury manager from previously executed deposit proposals (which reduce the treasury balance, but only after execution).

Because each proposal is validated independently against the instantaneous balance, an attacker (or a coalition of SNS token holders) can pass two proposals in sequence:

- **Proposal 1**: Deposit 49% of treasury → passes the 50% check, executes, treasury balance drops to 51%.
- **Proposal 2**: Deposit 49% of the *new* (reduced) balance → passes the 50% check again.

After two proposals, approximately 74% of the original treasury has been deposited. With more proposals, the entire treasury can be drained incrementally. The 50% limit is intended to be a meaningful safety cap, but it is only enforced per-transaction, not cumulatively.

The analogous pattern to the external report is exact: in the Blueberry report, `curPosSize` only reflected the current deposit's LP tokens rather than the total accumulated position. Here, `sns_balance` / `icp_balance` reflects the current treasury balance at validation time, not the total amount already committed to the treasury manager across all prior proposals.

### Impact Explanation

An SNS governance majority (or a coalition that can pass proposals) can incrementally drain the SNS or ICP treasury into a treasury manager canister beyond the intended 50% single-deposit safety limit. This undermines the treasury protection mechanism, potentially allowing the full treasury to be deposited into an external canister under the control of the treasury manager. If the treasury manager is malicious or compromised, this results in total loss of SNS treasury funds.

### Likelihood Explanation

This requires passing multiple SNS governance proposals, which requires a voting majority. However, the SNS governance model allows any neuron holder with sufficient voting power to submit and pass proposals. A malicious developer team or a coordinated group of token holders could exploit this. The attack is fully on-chain, requires no privileged access, and is reachable via the standard `ExecuteExtensionOperation` proposal path.

### Recommendation

The `validate_deposit_operation_impl` function should track the cumulative amount already deposited to the treasury manager (e.g., by querying the treasury manager's current balance or by recording total approved/deposited amounts in governance state) and subtract that from the treasury balance before applying the 50% check. Alternatively, the 50% limit should be applied against the **original** treasury balance (before any deposits), not the current post-deposit balance.

### Proof of Concept

1. SNS treasury has 1,000 SNS tokens and 2,000 ICP.
2. Attacker passes **Proposal A**: `treasury_allocation_sns_e8s = 490_000_000` (49% of 1,000). Validation passes (`490M ≤ 500M`). Execution runs `approve_treasury_manager` and calls `deposit`. Treasury now holds ~510 SNS tokens.
3. Attacker passes **Proposal B**: `treasury_allocation_sns_e8s = 249_900_000` (49% of ~510). Validation passes against the new balance. Execution deposits again. Treasury now holds ~260 SNS tokens.
4. Repeat until treasury is effectively drained.

The root cause is in `validate_deposit_operation_impl`: [1](#0-0) 

Each call reads the live balance and checks only the current proposal's amount, with no memory of prior deposits: [2](#0-1) 

The execution path that actually transfers funds is `execute_treasury_manager_deposit` → `approve_treasury_manager`, which issues an `icrc2_approve` for the full requested amount without any cumulative guard: [3](#0-2) [4](#0-3)

### Citations

**File:** rs/sns/governance/src/extensions.rs (L285-318)
```rust
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
```

**File:** rs/sns/governance/src/extensions.rs (L777-830)
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
```

**File:** rs/sns/governance/src/extensions.rs (L1566-1573)
```rust
    // 1. Transfer funds from treasury to treasury manager
    governance
        .approve_treasury_manager(
            extension_canister_id,
            treasury_allocation_sns_e8s,
            treasury_allocation_icp_e8s,
        )
        .await?;
```
