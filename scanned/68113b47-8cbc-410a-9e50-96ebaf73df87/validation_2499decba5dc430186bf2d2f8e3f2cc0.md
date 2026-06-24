### Title
Residual ICRC-2 Allowance Not Cleared After Treasury Manager Deposit — (File: `rs/sns/governance/src/extensions.rs`)

### Summary
The `execute_treasury_manager_deposit` function in `rs/sns/governance/src/extensions.rs` calls `approve_treasury_manager` to grant ICRC-2 allowances to a treasury manager canister on both the SNS and ICP ledgers, then calls `deposit` on the treasury manager. After the deposit call returns, no step revokes or zeroes out the residual allowance. Any unspent portion of the approved amount remains live on the ledger for up to one hour, giving the treasury manager canister an unintended ongoing authorization to pull additional SNS/ICP tokens from the SNS treasury subaccounts.

### Finding Description
`approve_treasury_manager` (lines 777–831) issues two `icrc2_approve` calls — one to the SNS ICRC-1 ledger and one to the ICP ledger — each with `expected_allowance: None` (blind overwrite) and an expiry of `now + ONE_HOUR_SECONDS`: [1](#0-0) [2](#0-1) 

`execute_treasury_manager_deposit` (lines 1546–1610) then calls `approve_treasury_manager` followed by `deposit` on the extension canister, but performs no post-deposit allowance revocation: [3](#0-2) 

The same pattern appears in `ValidatedRegisterExtension::execute` (lines 545–564), which calls `approve_treasury_manager` before installing the treasury manager WASM and never zeroes the allowance afterward: [4](#0-3) 

The ICRC-2 ledger's `use_allowance` only decrements the allowance by `amount + fee` per `transfer_from` call: [5](#0-4) 

If the treasury manager's `deposit` implementation calls `icrc2_transfer_from` for any amount strictly less than `approved_amount − fee`, a non-zero residual allowance remains on the ledger until the one-hour expiry elapses.

### Impact Explanation
During the one-hour window following each successful deposit proposal execution, the treasury manager canister retains a live ICRC-2 allowance over the SNS governance treasury subaccount and the ICP treasury account. A buggy or subsequently-upgraded treasury manager canister can call `icrc2_transfer_from` a second time against either ledger, draining the residual amount from the SNS treasury without any additional governance approval. Because the allowance is set with `expected_allowance: None`, a subsequent deposit proposal that fires within the same window will blindly overwrite the allowance rather than detecting the stale state, masking the residual exposure.

### Likelihood Explanation
The treasury manager is a governance-controlled canister installed on a fiduciary subnet, so direct exploitation requires either a bug in the treasury manager's `deposit` logic that leaves the allowance partially consumed, or a malicious upgrade of the treasury manager canister between the deposit call and the allowance expiry. Both scenarios are realistic given that the treasury manager WASM allowlist is externally maintained and the one-hour window is wide. The structural absence of a post-deposit zero-approval call means every deposit operation silently leaves an open authorization.

### Recommendation
After the `deposit` (and `install_code`) call returns — whether it succeeds or fails — call `approve_treasury_manager` with zero amounts to revoke both allowances:

```rust
// After deposit call
governance
    .approve_treasury_manager(extension_canister_id, 0, 0)
    .await
    .unwrap_or_else(|e| log!(ERROR, "Failed to clear residual allowance: {e}"));
```

Alternatively, pass `expected_allowance: Some(0)` on the next approval to detect and reject any unexpected residual state, consistent with the ICRC-2 CAS pattern already used elsewhere in the codebase. [6](#0-5) 

### Proof of Concept

1. An SNS governance proposal is submitted and executed: `execute_treasury_manager_deposit` with `treasury_allocation_sns_e8s = 1_000_000` and `treasury_allocation_icp_e8s = 500_000`.
2. `approve_treasury_manager` sets: SNS ledger allowance = 1_000_000 (expiry = now + 1 h), ICP ledger allowance = 500_000 (expiry = now + 1 h).
3. `deposit` is called on the treasury manager canister.
4. The treasury manager calls `icrc2_transfer_from(amount = 989_900)` on the SNS ledger; allowance consumed = 989_900 + 10_100 (fee) = 1_000_000 — full, no residual. **OR** the treasury manager calls `icrc2_transfer_from(amount = 979_900)` due to slippage/partial fill; allowance consumed = 979_900 + 10_100 = 990_000, leaving residual = 10_000.
5. `execute_treasury_manager_deposit` returns `Ok(())` with no cleanup.
6. Within the next hour, the treasury manager canister (or a re-entrant call triggered by a periodic task) calls `icrc2_transfer_from(amount = 9_900)` on the SNS ledger; allowance consumed = 9_900 + 100 = 10_000 — residual drained without any governance vote. [7](#0-6)

### Citations

**File:** rs/sns/governance/src/extensions.rs (L545-564)
```rust
                    governance
                        .approve_treasury_manager(
                            extension_canister_id,
                            treasury_allocation_sns_e8s,
                            treasury_allocation_icp_e8s,
                        )
                        .await?;

                    init_blob
                }
            };

            governance
                .upgrade_non_root_canister(
                    extension_canister_id,
                    wasm,
                    init_blob,
                    CanisterInstallMode::Install,
                )
                .await?;
```

**File:** rs/sns/governance/src/extensions.rs (L794-810)
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
```

**File:** rs/sns/governance/src/extensions.rs (L812-829)
```rust
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

```

**File:** rs/sns/governance/src/extensions.rs (L1546-1610)
```rust
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

    log!(
        INFO,
        "TreasuryManager.deposit succeeded with response: {:?}",
        balances
    );

    Ok(())
}
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

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L351-365)
```rust
                        let mut new_allowance = old_allowance.clone();
                        new_allowance.amount = old_allowance
                            .amount
                            .checked_sub(&amount)
                            .expect("Underflow when using allowance");
                        let rest = new_allowance.amount.clone();
                        if rest.is_zero() {
                            if let Some(expires_at) = old_allowance.expires_at {
                                table.allowances_data.remove_expiry(expires_at, key.clone());
                            }
                            table.allowances_data.remove_allowance(&key);
                        } else {
                            table.allowances_data.set_allowance(key, new_allowance);
                        }
                        Ok(rest)
```
