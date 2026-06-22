### Title
Unrevoked ICRC-2 Allowance After Treasury Manager Deposit Enables Over-Withdrawal from SNS Treasury - (File: `rs/sns/governance/src/extensions.rs`)

---

### Summary

The SNS Governance canister grants ICRC-2 allowances to the treasury manager extension canister before calling its `deposit` method, but never revokes the remaining unspent allowance after the call returns. This leaves a live allowance window (up to one hour) during which the treasury manager canister can call `icrc2_transfer_from` on the SNS and ICP ledgers to drain funds from the SNS treasury beyond what was consumed during the deposit operation.

---

### Finding Description

In `execute_treasury_manager_deposit`, the governance canister executes two steps:

1. **Step 1** — calls `approve_treasury_manager`, which issues `icrc2_approve` on both the SNS ledger and the ICP ledger, granting the treasury manager canister an allowance equal to the full `treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s` amounts, with a 1-hour expiry.

2. **Step 2** — calls `deposit` on the treasury manager canister. [1](#0-0) 

After `deposit` returns, **no step revokes the remaining allowance**. The function simply logs success and returns `Ok(())`.

The `approve_treasury_manager` helper sets the expiry to `now + ONE_HOUR_SECONDS` and passes `expected_allowance: None`, which means the ledger blindly overwrites any prior allowance — but it does not zero out the allowance after the deposit call completes. [2](#0-1) 

The same pattern exists in `ValidatedRegisterExtension::execute`, where `approve_treasury_manager` is called before `upgrade_non_root_canister` installs the treasury manager's Wasm. After the canister is installed and its `init` function runs, any unspent portion of the allowance persists for up to one hour. [3](#0-2) 

Because the treasury manager canister is the named `spender` in the ICRC-2 allowance, it is the only principal that can call `icrc2_transfer_from` against the SNS treasury subaccount and the ICP treasury subaccount using that allowance. If the treasury manager does not fully consume the approved amount during `deposit` (e.g., due to DEX slippage, partial execution, or a bug), the residual allowance remains live and callable. [4](#0-3) 

The ICRC-2 standard itself correctly decrements the allowance on each `transfer_from` call, but it does not automatically zero the allowance when the spender's operation completes — that responsibility lies with the approver (the governance canister), which never performs the cleanup. [5](#0-4) 

---

### Impact Explanation

A treasury manager canister that does not fully consume its approved allowance during `deposit` retains the ability to call `icrc2_transfer_from` on the SNS ledger and ICP ledger for the residual amount, for up to one hour after the governance proposal executes. This allows the treasury manager to withdraw SNS tokens and ICP from the governance treasury subaccounts beyond the amount actually deposited — without any additional governance approval. The governance canister has no mechanism to detect or prevent this secondary withdrawal. The maximum over-withdrawal is bounded by the approved amounts (`treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s`) minus what was consumed during `deposit`.

---

### Likelihood Explanation

The treasury manager is an NNS-blessed extension canister, which reduces but does not eliminate risk. Partial allowance consumption is a realistic outcome: the treasury manager DID specification explicitly acknowledges that DEX slippage can cause deposits to execute at different ratios than expected, meaning the treasury manager may legitimately be unable to deploy the full approved amount. In that case, the residual allowance is an unintended capability. A buggy treasury manager implementation could also re-invoke `icrc2_transfer_from` on the residual allowance without malicious intent. The 1-hour expiry window is a meaningful but incomplete mitigation. [6](#0-5) 

---

### Recommendation

After `deposit` returns (whether successfully or not), the governance canister should revoke the remaining allowance on both ledgers by calling `icrc2_approve` with `amount = 0` for the treasury manager as spender. This is the direct analog of the `safeApprove(target, 0)` fix in the referenced report. Concretely, `execute_treasury_manager_deposit` should call a new `revoke_treasury_manager_approval` helper immediately after the `deposit` call (or in a `finally`-style cleanup block), issuing:

```rust
self.ledger.icrc2_approve(to, 0, None, fee, sns_subaccount, None).await;
self.nns_ledger.icrc2_approve(to, 0, None, fee, icp_subaccount, None).await;
```

The same cleanup should be applied in `ValidatedRegisterExtension::execute` after `upgrade_non_root_canister` completes.

---

### Proof of Concept

1. A legitimate SNS governance proposal is passed to execute a `TreasuryManagerDeposit` operation for `X` SNS tokens and `Y` ICP tokens.
2. `execute_treasury_manager_deposit` calls `approve_treasury_manager`, granting the treasury manager an ICRC-2 allowance of `X` SNS and `Y` ICP with a 1-hour expiry.
3. The treasury manager's `deposit` method is called. Due to DEX slippage, it only pulls `X/2` SNS and `Y/2` ICP via `icrc2_transfer_from`.
4. `deposit` returns successfully. The governance canister logs success and returns — **without revoking the remaining `X/2` SNS and `Y/2` ICP allowance**.
5. Within the next hour, the treasury manager canister calls `icrc2_transfer_from` on the SNS ledger and ICP ledger directly, consuming the remaining `X/2` SNS and `Y/2` ICP from the governance treasury subaccounts.
6. The SNS treasury has lost `X` SNS and `Y` ICP in total, but the governance proposal only authorized a deposit of `X/2` SNS and `Y/2` ICP worth of value. [1](#0-0) [2](#0-1)

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

**File:** rs/sns/governance/src/extensions.rs (L1566-1609)
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

    log!(
        INFO,
        "TreasuryManager.deposit succeeded with response: {:?}",
        balances
    );

    Ok(())
```

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L232-250)
```rust
    /// Changes the spender's allowance for the account to the specified amount and expiration.
    pub fn approve(
        &mut self,
        account: &AD::AccountId,
        spender: &AD::AccountId,
        amount: AD::Tokens,
        expires_at: Option<TimeStamp>,
        now: TimeStamp,
        expected_allowance: Option<AD::Tokens>,
    ) -> Result<AD::Tokens, ApproveError<AD::Tokens>> {
        self.with_postconditions_check(|table| {
            if account == spender {
                return Err(ApproveError::SelfApproval);
            }

            if expires_at.unwrap_or_else(remote_future) <= now {
                return Err(ApproveError::ExpiredApproval { now });
            }

```

**File:** rs/sns/treasury_manager/treasury_manager.did (L35-41)
```text
// Known Security Risks:
// ====================
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.

```
