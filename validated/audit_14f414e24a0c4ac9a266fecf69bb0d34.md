### Title
SNS Governance `approve_treasury_manager` Allowance Reset Enables Treasury Over-Drain via Malicious Extension Canister - (File: `rs/sns/governance/src/extensions.rs`)

### Summary
The `approve_treasury_manager` function in SNS Governance calls `icrc2_approve` with `expected_allowance: None`, which blindly overwrites any existing ICRC-2 allowance. An inline comment incorrectly asserts this prevents double-spending. A malicious or compromised treasury manager extension canister can exploit the allowance reset across successive deposit proposals to drain more SNS tokens and ICP from the SNS treasury than any single proposal authorized.

### Finding Description

`approve_treasury_manager` is called during both extension registration (`ValidatedRegisterExtension::execute`) and deposit execution (`execute_treasury_manager_deposit`). In both cases it issues two sequential `icrc2_approve` calls — one to the SNS ledger and one to the ICP ledger — with `expected_allowance: None`: [1](#0-0) 

The comment at lines 791–792 reads:

> "If expected_allowance is None, the ledger *blindly* overwrites any existing allowance (even if non-zero). Therefore, there is no risk of double spending."

This reasoning is wrong. The overwrite does not prevent double-spending; it enables it. The ICRC-2 `approve` function, when called without `expected_allowance`, unconditionally replaces the stored allowance regardless of how much has already been consumed: [2](#0-1) 

The treasury manager canister is the designated spender. Nothing in the ICRC-2 protocol prevents the treasury manager from calling `icrc2_transfer_from` at any time — not only inside its `deposit` handler. The attack sequence is:

1. **Deposit Proposal A** passes. `approve_treasury_manager(X_sns, Y_icp)` is called → SNS allowance = X, ICP allowance = Y.
2. The treasury manager (malicious) calls `icrc2_transfer_from` for X SNS **before** the `deposit` call arrives, draining the SNS allowance to 0.
3. Governance calls `deposit` on the treasury manager. The treasury manager returns a failure (or partial success), so governance records the proposal as failed.
4. **Deposit Proposal B** passes. `approve_treasury_manager(X_sns, Y_icp)` is called again → SNS allowance is **reset** to X (overwriting 0 with X), ICP allowance = Y.
5. The treasury manager calls `icrc2_transfer_from` for X SNS again.
6. Total drained: **2X SNS** (and potentially 2Y ICP), while each individual proposal only authorized X SNS + Y ICP.

This is structurally identical to the Teller `updateCommitment` vulnerability: the "commitment" (ICRC-2 allowance) is reset by a new governance action, and the malicious spender exploits the reset to drain funds a second time.

The `execute_treasury_manager_deposit` flow makes the window explicit — there is an `await` between `approve_treasury_manager` and the `deposit` call, during which the treasury manager can act: [3](#0-2) 

A secondary, non-malicious variant also exists: two concurrent deposit proposals can interleave their `approve_treasury_manager` calls. Because `expected_allowance: None` causes a blind overwrite, Proposal B's `icrc2_approve` can silently replace Proposal A's allowance mid-flight, causing Proposal A's subsequent `deposit` call to fail with `InsufficientAllowance` even though the treasury had sufficient funds when Proposal A was validated.

### Impact Explanation
A malicious or compromised treasury manager extension canister can drain more SNS tokens and ICP from the SNS treasury than any single governance proposal authorized. Across N deposit proposals, the treasury manager can drain N × (proposal amount) while each proposal individually passes the ≤50% balance validation check. The SNS treasury — which holds community funds — can be fully emptied without any single proposal exceeding the per-proposal cap.

### Likelihood Explanation
The treasury manager must be registered via a governance proposal. However, once registered, the canister can exploit the allowance reset autonomously across future deposit proposals without any further governance action. The risk is realistic in scenarios where: (a) a treasury manager implementation contains a bug that causes premature `transfer_from` calls, or (b) a treasury manager canister is compromised after registration. The DID file itself acknowledges known security risks in the treasury manager design. [4](#0-3) 

### Recommendation
Replace the `expected_allowance: None` argument in both `icrc2_approve` calls inside `approve_treasury_manager` with the current on-chain allowance read immediately before the approve, using it as a compare-and-swap guard. This ensures the approve fails (returning `AllowanceChanged`) if the treasury manager has already consumed part of the allowance since the last approval, preventing the reset from being exploited:

```rust
// Before approving, read the current allowance.
let current_sns_allowance = self.ledger.allowance(treasury_subaccount, spender).await?;
self.ledger.icrc2_approve(
    to,
    sns_amount_e8s,
    Some(expiry_time_nsec),
    fee,
    self.sns_treasury_subaccount(),
    Some(current_sns_allowance.amount),  // CAS guard
).await?;
```

Alternatively, enforce a single-active-deposit invariant in governance state so that no new deposit proposal can be executed while a prior one is in flight.

### Proof of Concept

**Setup**: Register a treasury manager extension canister `TM` that, upon receiving any `deposit` call, first calls `icrc2_transfer_from` to drain the full SNS allowance, then returns `Err(...)`.

**Step 1**: Pass SNS governance Proposal A: `ExecuteExtensionOperation { deposit, treasury_allocation_sns_e8s: 50_000_000 }`.

**Step 2**: Governance executes `approve_treasury_manager(50_000_000, ...)`:
- SNS ledger allowance for `TM` = 50_000_000.

**Step 3**: Governance calls `TM.deposit(...)`. `TM` immediately calls `icrc2_transfer_from` for 50_000_000 SNS → allowance = 0, `TM` holds 50_000_000 SNS. `TM` returns `Err(...)`.

**Step 4**: Proposal A is marked failed. SNS treasury has lost 50_000_000 SNS.

**Step 5**: Pass Proposal B: same parameters. Governance calls `approve_treasury_manager(50_000_000, ...)` → SNS allowance **reset** to 50_000_000 (overwrites 0).

**Step 6**: Governance calls `TM.deposit(...)`. `TM` drains 50_000_000 SNS again.

**Result**: 100_000_000 SNS drained across two proposals, each of which individually authorized only 50_000_000 SNS. The 50% per-proposal cap is bypassed. [5](#0-4) [6](#0-5) [7](#0-6)

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

**File:** rs/sns/governance/src/extensions.rs (L1545-1610)
```rust
/// Execute a treasury manager deposit operation
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

**File:** rs/sns/treasury_manager/treasury_manager.did (L35-40)
```text
// Known Security Risks:
// ====================
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.
```
