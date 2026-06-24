### Title
Incorrect `expected_allowance` Reasoning Enables Allowance Double-Spend in SNS Treasury Manager Approval - (File: `rs/sns/governance/src/extensions.rs`)

### Summary

The `approve_treasury_manager` function in SNS Governance calls `icrc2_approve` with `expected_allowance: None` and contains a comment that incorrectly claims this eliminates double-spend risk. In reality, omitting `expected_allowance` is precisely what enables the classic allowance double-spend: a treasury manager canister that retains an unspent prior allowance can drain it before the new approve overwrites it, then drain the new allowance immediately after — spending both.

### Finding Description

In `rs/sns/governance/src/extensions.rs`, the `approve_treasury_manager` function issues two `icrc2_approve` calls (one to the SNS ledger, one to the ICP ledger) with `expected_allowance: None`:

```rust
// If expected_allowance is None, the ledger *blindly* overwrites any existing
// allowance (even if non-zero). Therefore, there is no risk of double spending.

self.ledger
    .icrc2_approve(
        to,
        sns_amount_e8s,
        Some(expiry_time_nsec),
        self.transaction_fee_e8s_or_panic(),
        self.sns_treasury_subaccount(),
        None,   // <-- expected_allowance: None
    )
    .await ...

self.nns_ledger
    .icrc2_approve(
        to,
        icp_amount_e8s,
        Some(expiry_time_nsec),
        icp_ledger::DEFAULT_TRANSFER_FEE.get_e8s(),
        self.icp_treasury_subaccount(),
        None,   // <-- expected_allowance: None
    )
    .await ...
``` [1](#0-0) 

The comment's reasoning is **inverted**. The blind overwrite does not prevent double-spending — it is the mechanism that enables it. The `expected_allowance` field in ICRC-2 exists specifically as the mitigation for this class of attack:

```rust
// In approvals.rs: when expected_allowance is None, the check is skipped entirely
if let Some(expected_allowance) = expected_allowance {
    // ... check current allowance matches expected ...
    if expected_allowance != current_allowance {
        return Err(ApproveError::AllowanceChanged { current_allowance });
    }
}
// Falls through to unconditional set_allowance when None
table.allowances_data.set_allowance(key.clone(), Allowance { amount: amount.clone(), ... });
``` [2](#0-1) 

The `execute_treasury_manager_deposit` function shows the two-step flow: first `approve_treasury_manager` sets the allowance, then `deposit` is called on the treasury manager canister: [3](#0-2) 

Between successive deposit proposals, the treasury manager canister holds a live allowance. Because `expected_allowance: None` is used, the governance canister never verifies that the prior allowance was fully consumed before issuing a new one.

### Impact Explanation

A malicious or compromised treasury manager canister can execute the following double-spend:

1. **Deposit proposal 1** is executed: governance calls `approve_treasury_manager(X_sns, Y_icp)` → allowance set to X SNS + Y ICP. `deposit` is called; treasury manager receives the call but deliberately does **not** spend the allowance (or spends only part of it).
2. **Deposit proposal 2** is executed: governance calls `approve_treasury_manager(X'_sns, Y'_icp)` with `expected_allowance: None`. The ledger blindly overwrites the allowance to X' SNS + Y' ICP.
3. **Between steps 1 and 2**, the treasury manager calls `icrc2_transfer_from` to drain the old allowance (X SNS + Y ICP) from the SNS treasury subaccount.
4. **After step 2**, the treasury manager calls `icrc2_transfer_from` again to drain the new allowance (X' SNS + Y' ICP).

Total drained: **(X + X') SNS tokens** and **(Y + Y') ICP** — double what governance intended to authorize.

The SNS treasury subaccounts hold real DAO funds. The `ApproveError::AllowanceChanged` guard that ICRC-2 provides is never invoked because `expected_allowance` is `None`. [4](#0-3) 

### Likelihood Explanation

The treasury manager canister is an external canister registered via governance proposal and is expected to be NNS-blessed. However:

- The incorrect comment demonstrates the developers believe the current code is safe, meaning no compensating controls were added.
- The treasury manager's `deposit` function is called **after** the allowance is set, giving the canister a window to withhold spending the old allowance.
- A compromised, buggy, or maliciously upgraded treasury manager canister (upgrades can be proposed through governance) can exploit this without any privileged access beyond the allowance already granted.
- The attack requires no mempool monitoring (unlike Ethereum front-running): the treasury manager simply delays spending the old allowance until after the new approve is issued.

Likelihood: **Medium** — requires a malicious or compromised treasury manager, but the incorrect comment means no protocol-level guard prevents it.

### Recommendation

Replace `None` with `Some(0)` for `expected_allowance` in both `icrc2_approve` calls inside `approve_treasury_manager`. This enforces that the prior allowance must be fully consumed (zero) before a new one is granted, making the double-spend impossible:

```rust
self.ledger
    .icrc2_approve(
        to,
        sns_amount_e8s,
        Some(expiry_time_nsec),
        self.transaction_fee_e8s_or_panic(),
        self.sns_treasury_subaccount(),
        Some(0),  // enforce prior allowance is zero
    )
    .await ...
```

If a non-zero residual allowance is expected (e.g., from a partially-used prior deposit), the governance canister should first query the current allowance via `icrc2_allowance` and pass it as `expected_allowance`, or explicitly revoke the old allowance (set to 0) before issuing the new one.

Also remove or correct the misleading comment at line 791–792.

### Proof of Concept

```
Round 1:
  SNS Governance → icrc2_approve(treasury_manager, sns=1000, icp=500, expected_allowance=None)
  Ledger: allowance[treasury_sns_subaccount → treasury_manager] = 1000 SNS
  Ledger: allowance[treasury_icp_subaccount → treasury_manager] = 500 ICP
  SNS Governance → treasury_manager.deposit(...)
  Treasury manager: receives deposit call, does NOT call icrc2_transfer_from yet

Round 2 (new governance proposal):
  SNS Governance → icrc2_approve(treasury_manager, sns=2000, icp=1000, expected_allowance=None)
  [Before this approve is processed, treasury manager calls:]
  Treasury manager → icrc2_transfer_from(from=treasury_sns_subaccount, amount=1000 SNS)
  Treasury manager → icrc2_transfer_from(from=treasury_icp_subaccount, amount=500 ICP)
  [Approve is now processed — ledger blindly overwrites:]
  Ledger: allowance[treasury_sns_subaccount → treasury_manager] = 2000 SNS
  Ledger: allowance[treasury_icp_subaccount → treasury_manager] = 1000 ICP
  [Treasury manager drains new allowance:]
  Treasury manager → icrc2_transfer_from(from=treasury_sns_subaccount, amount=2000 SNS)
  Treasury manager → icrc2_transfer_from(from=treasury_icp_subaccount, amount=1000 ICP)

Total stolen: 3000 SNS + 1500 ICP (governance only intended to authorize 2000 SNS + 1000 ICP)
```

The root cause is confirmed at:
- [5](#0-4) 
- [6](#0-5)

### Citations

**File:** rs/sns/governance/src/extensions.rs (L791-820)
```rust
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

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L278-307)
```rust
                Some(old_allowance) => {
                    if let Some(expected_allowance) = expected_allowance {
                        let current_allowance = if let Some(expires_at) = old_allowance.expires_at {
                            if expires_at <= now {
                                AD::Tokens::zero()
                            } else {
                                old_allowance.amount.clone()
                            }
                        } else {
                            old_allowance.amount.clone()
                        };
                        if expected_allowance != current_allowance {
                            return Err(ApproveError::AllowanceChanged { current_allowance });
                        }
                    }
                    if amount == AD::Tokens::zero() {
                        if let Some(expires_at) = old_allowance.expires_at {
                            table.allowances_data.remove_expiry(expires_at, key.clone());
                        }
                        table.allowances_data.remove_allowance(&key);
                        return Ok(amount);
                    }
                    table.allowances_data.set_allowance(
                        key.clone(),
                        Allowance {
                            amount: amount.clone(),
                            expires_at,
                            arrived_at: now,
                        },
                    );
```

**File:** packages/icrc-ledger-types/src/icrc2/approve.rs (L17-18)
```rust
    #[serde(default)]
    pub expected_allowance: Option<Nat>,
```
