### Title
Missing Allowance Revocation in `execute_treasury_manager_withdraw` Leaves Residual ICRC-2 Allowances Active - (File: rs/sns/governance/src/extensions.rs)

### Summary
`execute_treasury_manager_deposit` sets ICRC-2 allowances on both the SNS and ICP ledgers via `approve_treasury_manager()`, but `execute_treasury_manager_withdraw` performs no corresponding revocation of those allowances. After a withdraw is executed, any residual allowance from a prior deposit that was not fully consumed remains live on the ledger for up to one hour, allowing the treasury manager canister to pull additional SNS/ICP tokens from the governance treasury without a new governance proposal.

### Finding Description
In `rs/sns/governance/src/extensions.rs`, `execute_treasury_manager_deposit` (lines 1545–1610) performs two steps:

1. Calls `governance.approve_treasury_manager(extension_canister_id, sns_amount_e8s, icp_amount_e8s)` — this issues `icrc2_approve` on both the SNS ledger and the ICP ledger, granting the treasury manager canister a spending allowance that expires in one hour.
2. Calls `extension_canister_id.deposit(arg_blob)` — the treasury manager pulls funds using `icrc2_transfer_from`. [1](#0-0) 

`execute_treasury_manager_withdraw` (lines 1612–1661) only calls `extension_canister_id.withdraw(arg_blob)`. It performs **no revocation** of any existing ICRC-2 allowances that the treasury manager holds on the SNS or ICP ledger. [2](#0-1) 

`approve_treasury_manager` sets allowances with `expiry_time_sec = self.env.now().saturating_add(ONE_HOUR_SECONDS)` and `expected_allowance: None` (blind overwrite). [3](#0-2) 

The `TreasuryManager` trait mandates a `refresh_balances()` and `issue_rewards()` method, both called periodically (every hour in the mock). A real implementation of either method could invoke `icrc2_transfer_from` against the residual allowance. [4](#0-3) [5](#0-4) 

### Impact Explanation
After a deposit proposal executes, if the treasury manager only partially consumes the approved allowance (e.g., due to slippage, partial liquidity, or a partial failure), a residual allowance remains on the SNS and/or ICP ledger. When a subsequent withdraw proposal executes, `execute_treasury_manager_withdraw` does not zero out these residual allowances. During the remaining window of the one-hour expiry, the treasury manager's periodic tasks (`issue_rewards`, `refresh_balances`) could invoke `icrc2_transfer_from` and pull additional SNS/ICP tokens from the governance treasury subaccount without any new governance proposal being approved. This constitutes an unauthorized transfer of SNS DAO treasury funds.

### Likelihood Explanation
The scenario requires: (1) a deposit proposal that does not fully consume the approved allowance, (2) a withdraw proposal executed within the same one-hour window, and (3) a treasury manager implementation whose periodic tasks use `icrc2_transfer_from` against the treasury. Condition (1) is realistic given the documented slippage risk. Condition (2) is plausible in active DAOs. Condition (3) depends on the specific treasury manager implementation but is architecturally permitted by the `TreasuryManager` trait. The one-hour expiry limits the window but does not eliminate it.

### Recommendation
After a successful `withdraw` call in `execute_treasury_manager_withdraw`, revoke any outstanding ICRC-2 allowances by calling `icrc2_approve` with `amount = 0` on both the SNS ledger and the ICP ledger for the treasury manager canister. Similarly, on failure of `execute_treasury_manager_deposit` after `approve_treasury_manager()` has already succeeded, a cleanup step should revoke the newly set allowances. This mirrors the cleanup pattern already present in `ValidatedRegisterExtension::execute`, which calls `clean_up_failed_register_extension` on failure. [6](#0-5) 

### Proof of Concept
1. SNS governance executes a `TreasuryManagerDeposit` proposal for X SNS tokens and Y ICP tokens.
2. `approve_treasury_manager()` sets allowances: treasury → treasury_manager for X SNS and Y ICP (expires T+1h).
3. `deposit()` is called; the treasury manager pulls only X/2 SNS tokens due to slippage. Residual allowance of X/2 SNS remains.
4. SNS governance executes a `TreasuryManagerWithdraw` proposal. `execute_treasury_manager_withdraw` calls `withdraw()` and returns `Ok`. No allowance revocation occurs.
5. Within the remaining window before T+1h, the treasury manager's `run_periodic_tasks` fires. If `issue_rewards()` calls `icrc2_transfer_from(treasury_subaccount, treasury_manager, X/2)`, it succeeds — pulling X/2 SNS tokens from the governance treasury without any governance proposal authorizing this transfer. [2](#0-1) [7](#0-6)

### Citations

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

**File:** rs/sns/governance/src/extensions.rs (L1612-1661)
```rust
/// Execute a treasury manager withdraw operation
async fn execute_treasury_manager_withdraw(
    governance: &Governance,
    extension_canister_id: CanisterId,
    arg: ValidatedWithdrawOperationArg,
) -> Result<(), GovernanceError> {
    let arg_blob = construct_treasury_manager_withdraw_payload(arg.original).map_err(|err| {
        GovernanceError::new_with_message(
            ErrorType::PreconditionFailed,
            format!("Failed to construct treasury manager withdraw payload: {err}"),
        )
    })?;

    let balances = governance
        .env
        .call_canister(extension_canister_id, "withdraw", arg_blob)
        .await
        .map_err(|(code, err)| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!(
                    "Canister method call {extension_canister_id}.withdraw failed with code {code:?}: {err}"
                ),
            )
        })
        .and_then(|blob| {
            Decode!(&blob, sns_treasury_manager::TreasuryManagerResult).map_err(|err| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!(
                        "Error decoding TreasuryManager.withdraw response: {err:?}"
                    ),
                )
            })
        })?
        .map_err(|err| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!("TreasuryManager.withdraw failed: {err:?}"),
            )
        })?;

    log!(
        INFO,
        "TreasuryManager.withdraw succeeded with response: {:?}",
        balances
    );

    Ok(())
}
```

**File:** rs/sns/treasury_manager/src/lib.rs (L272-282)
```rust
    /// Context: the source of truth for balances are some remote canisters (e.g., the ledgers).
    /// The Treasury Manager needs to have a local cache of these balances to be able to make
    /// important decisions, e.g., how much can be refunded / withdrawn. That cache should be
    /// regularly updated, and this is the function that should do that.
    ///
    /// Should not be exposed as an API function, but rather called periodically by the canister.
    fn refresh_balances(&mut self) -> impl std::future::Future<Output = ()> + Send;

    /// Should not be exposed as an API function, but rather called periodically by the canister.
    fn issue_rewards(&mut self) -> impl std::future::Future<Output = ()> + Send;
}
```

**File:** rs/sns/treasury_manager/mock/src/main.rs (L99-107)
```rust
async fn run_periodic_tasks() {
    log("run_periodic_tasks.");

    let mut state = canister_state();

    state.refresh_balances().await;

    state.issue_rewards().await;
}
```
