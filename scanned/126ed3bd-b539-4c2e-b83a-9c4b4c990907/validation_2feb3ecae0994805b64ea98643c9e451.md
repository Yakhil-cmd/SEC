### Title
ICRC-2 Treasury Allowances Not Revoked on Failed Deposit — (`File: rs/sns/governance/src/extensions.rs`)

### Summary
In `execute_treasury_manager_deposit`, the SNS Governance canister grants ICRC-2 allowances to the treasury manager extension canister on both the SNS token ledger and the ICP ledger before calling `deposit`. If the `deposit` call fails for any reason, the function returns an error but never revokes the previously granted allowances. The treasury manager retains a live, spendable allowance for up to one hour, allowing it to pull SNS treasury funds without a corresponding successful governance proposal.

### Finding Description
`execute_treasury_manager_deposit` in `rs/sns/governance/src/extensions.rs` follows a two-step pattern:

**Step 1 — Grant allowances:** [1](#0-0) 

`approve_treasury_manager` issues `icrc2_approve` calls on both the SNS ledger and the ICP ledger, granting the treasury manager canister the right to spend up to `treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s` respectively, with an expiry of `now + ONE_HOUR_SECONDS`: [2](#0-1) 

**Step 2 — Call deposit:** [3](#0-2) 

If the `deposit` call fails (canister reject, decode error, or application-level error), the function propagates the error and returns. There is no code path that revokes the allowances granted in Step 1. The treasury manager canister retains valid ICRC-2 allowances on both ledgers for up to one hour.

The same pattern exists in `ValidatedRegisterExtension::execute` — allowances are granted before `upgrade_non_root_canister` is called, and the error-path cleanup (`clean_up_failed_register_extension`) only deregisters and deletes the canister; it does not revoke ledger allowances: [4](#0-3) [5](#0-4) 

### Impact Explanation
The treasury manager canister retains a live ICRC-2 allowance for up to one hour after a failed deposit. During this window it can call `icrc2_transfer_from` on the SNS token ledger and the ICP ledger to pull up to the full approved amounts from the SNS governance treasury subaccounts, without any corresponding successful governance proposal authorizing the transfer. This is a direct ledger conservation violation: SNS treasury funds can be moved without a valid, executed governance decision.

### Likelihood Explanation
A deposit proposal can fail due to a transient inter-canister call error, a bug in the treasury manager's `deposit` implementation, or a deliberately crafted failure. The treasury manager canister is installed via governance proposal and controlled by SNS Root, but its Wasm code is arbitrary. A treasury manager with a bug or malicious logic that causes `deposit` to return an error while retaining the allowance for later use is a realistic scenario. No malicious governance majority is required — only a passed deposit proposal (normal operation) followed by a failed `deposit` call.

### Recommendation
After a failed `deposit` call in `execute_treasury_manager_deposit`, immediately revoke the granted allowances by calling `icrc2_approve` with `amount = 0` on both the SNS ledger and the ICP ledger for the treasury manager canister. Similarly, in `ValidatedRegisterExtension::execute`, the `clean_up_failed_register_extension` path should also revoke any allowances granted before the failed install. A helper analogous to `approve_treasury_manager` but setting the amount to zero should be added and called in all error paths.

### Proof of Concept
1. An SNS governance proposal of type `ExecuteExtensionOperation` with `operation_name = "deposit"` and non-zero `treasury_allocation_sns_e8s` / `treasury_allocation_icp_e8s` is adopted and executed.
2. `execute_treasury_manager_deposit` calls `approve_treasury_manager`, which issues `icrc2_approve` on both ledgers granting the treasury manager canister allowances expiring in one hour. [6](#0-5) 
3. The subsequent `call_canister(extension_canister_id, "deposit", ...)` call returns an error (e.g., the treasury manager's `deposit` method traps or returns `Err`). [7](#0-6) 
4. `execute_treasury_manager_deposit` returns `Err(...)`. No allowance revocation occurs.
5. Within the one-hour expiry window, the treasury manager canister calls `icrc2_transfer_from` on the SNS ledger and ICP ledger, pulling up to the full approved amounts from the SNS governance treasury subaccounts — without any valid, executed governance authorization for the transfer.

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

**File:** rs/sns/governance/src/extensions.rs (L583-594)
```rust
        let main_result = main().await;

        // Try to clean up if main_result is Err. Cleaning up consists of
        // calling the Root canister's clean_up_failed_register_extension method.
        if main_result.is_err() {
            governance
                .clean_up_failed_register_extension(self.extension_canister_id)
                .await;
        }

        main_result
    }
```

**File:** rs/sns/governance/src/extensions.rs (L788-830)
```rust
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

**File:** rs/sns/governance/src/extensions.rs (L1575-1601)
```rust
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
