### Title
SNS Treasury Manager Allowance Frontrunning via Missing `expected_allowance` in `approve_treasury_manager` - (File: `rs/sns/governance/src/extensions.rs`)

### Summary
`approve_treasury_manager` in `rs/sns/governance/src/extensions.rs` calls `icrc2_approve` with `expected_allowance = None` for both the SNS and ICP ledgers. The inline comment claims "there is no risk of double spending," but this is incorrect. A malicious treasury manager canister can race between two successive governance-approved `approve_treasury_manager` executions — consuming the first allowance before the second overwrites it — and thereby drain `old_amount + new_amount` tokens from the SNS treasury instead of only `new_amount`.

### Finding Description
`approve_treasury_manager` is the direct IC analog of the `_addInvestor` / `migrate()` pattern in the original report. It sets an ICRC-2 allowance on both the SNS ledger and the ICP ledger for a registered treasury manager canister, passing `None` as `expected_allowance` in both calls: [1](#0-0) 

The ICRC-2 `AllowanceTable::approve` in `rs/ledger_suite/common/ledger_core/src/approvals.rs` only enforces the CAS-style check when `expected_allowance` is `Some(...)`: [2](#0-1) 

When `expected_allowance` is `None`, the function unconditionally overwrites the stored allowance with the new value: [3](#0-2) 

The `ApproveArgs` struct in `packages/icrc-ledger-types/src/icrc2/approve.rs` exposes `expected_allowance` as an optional field precisely to prevent this class of race condition: [4](#0-3) 

The comment's claim that "there is no risk of double spending" is only true if the treasury manager has not yet consumed the first allowance. If the treasury manager calls `icrc2_transfer_from` between the two governance proposals, it consumes the first allowance and then consumes the second allowance after the blind overwrite, receiving `old_amount + new_amount` instead of only `new_amount`. This is the exact same root cause as the ERC-20 approval frontrunning problem.

### Impact Explanation
A malicious or compromised treasury manager canister can drain `old_allowance + new_allowance` tokens from the SNS treasury subaccount and/or the ICP treasury subaccount instead of only `new_allowance`. Because the SNS treasury holds the protocol's native token reserves and ICP, this constitutes a direct ledger conservation violation: tokens leave the treasury subaccounts without a corresponding governance-authorized disbursement. The impact is proportional to the size of the first allowance set before the correction proposal.

### Likelihood Explanation
The attack requires two conditions:
1. The SNS governance passes a second `approve_treasury_manager` proposal for the same treasury manager canister (e.g., to correct an over-allocation or to update the amount after partial use).
2. The treasury manager canister is malicious or controlled by an attacker who monitors the governance canister's message queue.

Condition 1 is realistic: governance DAOs routinely issue correction proposals. Condition 2 is realistic because the treasury manager is an external canister whose controller is not necessarily the SNS governance itself. A treasury manager canister that front-runs the second proposal by calling `icrc2_transfer_from` immediately after detecting the first proposal's execution is a straightforward on-chain attack requiring no off-chain coordination — the treasury manager can poll the governance canister or use `read_state` to observe proposal execution.

### Recommendation
Pass the current on-chain allowance as `expected_allowance` when calling `icrc2_approve` in `approve_treasury_manager`. Before issuing the new approval, query `icrc2_allowance` to obtain the current value and pass it as `expected_allowance`. If the treasury manager has already consumed part of the allowance between the query and the approve, the ledger will return `AllowanceChanged` and the governance can retry. This is the same CAS-style fix that the ICRC-2 standard provides via `expected_allowance` and that the IC ledger already implements correctly in `AllowanceTable::approve`. [5](#0-4) 

Alternatively, always set the allowance to zero first (by calling `icrc2_approve` with `amount = 0` and `expected_allowance = None`) before setting the new value, mirroring the ERC-20 "reduce to zero first" pattern.

### Proof of Concept
```
t0: SNS governance executes proposal A:
    approve_treasury_manager(TM, sns=100, icp=100)
    → SNS ledger: allowance[treasury_sns_subaccount → TM] = 100
    → ICP ledger: allowance[treasury_icp_subaccount → TM] = 100

t1: TM (malicious) calls icrc2_transfer_from(treasury_sns_subaccount, TM, 100)
    → allowance[treasury_sns_subaccount → TM] = 0
    → TM receives 100 SNS tokens

t2: SNS governance executes proposal B (correction/reduction):
    approve_treasury_manager(TM, sns=50, icp=50)
    → SNS ledger: allowance[treasury_sns_subaccount → TM] = 50  (blind overwrite, no check)
    → ICP ledger: allowance[treasury_icp_subaccount → TM] = 50  (blind overwrite, no check)

t3: TM calls icrc2_transfer_from(treasury_sns_subaccount, TM, 50)
    → allowance[treasury_sns_subaccount → TM] = 0
    → TM receives 50 SNS tokens

Result: TM received 150 SNS; governance intended only 50 SNS.
        The same attack applies symmetrically to the ICP allowance.
```

The treasury manager canister observes proposal B's execution on-chain and times its `icrc2_transfer_from` call at t1 to precede proposal B's execution at t2. No off-chain infrastructure is required. The entry path is an unprivileged canister caller (`icrc2_transfer_from` on the SNS/ICP ledger) triggered by the treasury manager canister, which is within the stated scope of "malicious canister." [6](#0-5)

### Citations

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

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L253-261)
```rust
            match table.allowances_data.get_allowance(&key) {
                None => {
                    if let Some(expected_allowance) = expected_allowance
                        && !expected_allowance.is_zero()
                    {
                        return Err(ApproveError::AllowanceChanged {
                            current_allowance: AD::Tokens::zero(),
                        });
                    }
```

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L278-292)
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
```

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L300-307)
```rust
                    table.allowances_data.set_allowance(
                        key.clone(),
                        Allowance {
                            amount: amount.clone(),
                            expires_at,
                            arrived_at: now,
                        },
                    );
```

**File:** packages/icrc-ledger-types/src/icrc2/approve.rs (L17-19)
```rust
    #[serde(default)]
    pub expected_allowance: Option<Nat>,
    #[serde(default)]
```
