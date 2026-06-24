### Title
Unrevoked ICRC-2 Allowance After Failed Treasury Manager Deposit Enables Residual Fund Drain - (File: rs/sns/governance/src/extensions.rs)

---

### Summary

In `execute_treasury_manager_deposit`, SNS Governance grants an ICRC-2 allowance to the treasury manager canister before calling `deposit`. If the `deposit` call fails for any reason, the allowance is **never revoked**. The treasury manager retains a live 1-hour ICRC-2 allowance on both the SNS token ledger and the ICP ledger, which it can use to call `icrc2_transfer_from` and drain treasury funds — even though governance believes the deposit failed and no funds were moved.

---

### Finding Description

`execute_treasury_manager_deposit` in `rs/sns/governance/src/extensions.rs` executes a two-step sequence:

**Step 1** — `approve_treasury_manager` is called, which issues two `icrc2_approve` calls (one on the SNS ledger, one on the ICP ledger), granting the treasury manager canister an allowance expiring in exactly one hour:

```rust
let expiry_time_sec = self.env.now().saturating_add(ONE_HOUR_SECONDS);
let expiry_time_nsec = expiry_time_sec.saturating_mul(NANO_SECONDS_PER_SECOND);

self.ledger.icrc2_approve(to, sns_amount_e8s, Some(expiry_time_nsec), ...).await?;
self.nns_ledger.icrc2_approve(to, icp_amount_e8s, Some(expiry_time_nsec), ...).await?;
```

**Step 2** — `call_canister(extension_canister_id, "deposit", arg_blob)` is called. If this call fails (canister trap, error response, or inter-canister rejection), the function returns `Err(...)` to the caller. **No cleanup of the allowance is performed.**

```rust
// 1. Transfer funds from treasury to treasury manager
governance.approve_treasury_manager(...).await?;

// 2. Call deposit on treasury manager
let balances = governance.env.call_canister(extension_canister_id, "deposit", arg_blob)
    .await
    .map_err(|(code, err)| { ... })?   // <-- returns Err, allowance NOT revoked
    ...?;
```

The treasury manager canister — which has its own Wasm installed and is a live canister — still holds a valid ICRC-2 allowance on both ledgers for up to one hour. It can call `icrc2_transfer_from` directly on the SNS ledger and ICP ledger at any time within that window, bypassing the governance deposit flow entirely.

The same pattern exists in `ValidatedRegisterExtension::execute` (lines 545–551), where `approve_treasury_manager` is called before `upgrade_non_root_canister`. If the Wasm install fails, `clean_up_failed_register_extension` is called but **does not revoke the allowance**. In that specific case the canister has no code installed so the allowance cannot be used, but the structural gap is identical.

The comment in `approve_treasury_manager` explicitly notes that `expected_allowance = None` causes the ledger to blindly overwrite any existing allowance. This means a subsequent governance proposal cannot detect or correct a stale allowance left by a prior failed deposit.

---

### Impact Explanation

**High.** A treasury manager canister that returns an error from `deposit` (whether due to a bug, an external DEX failure, or deliberate misbehavior) retains a live ICRC-2 allowance on both the SNS token ledger and the ICP ledger. It can call `icrc2_transfer_from` on either ledger to move up to the approved amount out of the SNS treasury subaccount and the ICP treasury account to any destination it chooses. The SNS governance canister, having received an error from the deposit call, has no record that the allowance is still active and takes no corrective action. The treasury funds are at risk for the full one-hour allowance window.

---

### Likelihood Explanation

**Low.** Treasury manager canisters must be NNS-blessed and are registered via an SNS governance proposal. A purely malicious treasury manager requires a governance majority to register. However, a legitimately registered but buggy treasury manager (e.g., one whose `deposit` function fails due to an external DEX being unavailable, a decoding error, or an internal invariant violation) would leave the allowance active without any malicious intent. The 1-hour window is short but non-zero, and the allowance covers up to 50% of the current treasury balance (the maximum validated by `validate_deposit_operation_impl`).

---

### Recommendation

After a failed `deposit` call in `execute_treasury_manager_deposit`, immediately revoke the ICRC-2 allowance on both ledgers by calling `icrc2_approve` with `amount = 0` for the treasury manager canister. This mirrors the "forbid the approve/transfer functionality altogether" recommendation from the external report — if the deposit did not succeed, the approval should not persist.

Similarly, in `ValidatedRegisterExtension::execute`, if `upgrade_non_root_canister` fails, the cleanup path should also revoke any allowance granted in the preceding `approve_treasury_manager` call, even though the canister has no code at that point (defense in depth).

---

### Proof of Concept

1. An SNS passes a `TreasuryManagerDeposit` proposal targeting a registered treasury manager canister.
2. `execute_treasury_manager_deposit` is called by SNS Governance.
3. `approve_treasury_manager` succeeds: the SNS ledger and ICP ledger now record an allowance of `(sns_amount_e8s, icp_amount_e8s)` for the treasury manager canister, expiring in 1 hour.
4. `call_canister(extension_canister_id, "deposit", arg_blob)` is called. The treasury manager's `deposit` method traps or returns `Err(...)` (e.g., because the external DEX is down).
5. `execute_treasury_manager_deposit` returns `Err(GovernanceError { ... })`. SNS Governance logs the failure. No allowance revocation occurs.
6. Within the next hour, the treasury manager canister calls `icrc2_transfer_from` on the SNS ledger with `from = sns_governance_treasury_subaccount`, `to = attacker_account`, `amount = sns_amount_e8s`. The ledger validates the allowance and executes the transfer.
7. The treasury manager similarly drains the ICP allowance via the ICP ledger.
8. SNS treasury funds are drained. SNS Governance has no on-chain record that the allowance was used, since the deposit proposal was marked as failed.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** rs/sns/governance/src/extensions.rs (L276-320)
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
```

**File:** rs/sns/governance/src/extensions.rs (L545-594)
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

            let extension_name = spec.name.clone();
            cache_registered_extension(extension_canister_id, spec);

            // Inject fault, i.e. when there is a test that tries to force us to
            // explode, return Err.
            if cfg!(any(test, feature = "test")) && extension_name.contains("Explode in Test") {
                return Err(GovernanceError::new_with_message(
                    ErrorType::External,
                    "Something has gone terribly terribly wrong. Actually, this is just \
                     an injected fault. This would only appear in tests."
                        .to_string(),
                ));
            }

            Ok(())
        };

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
