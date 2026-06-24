### Title
SNS Governance `approve_treasury_manager` Sets Allowance Without `expected_allowance` Guard, Enabling Stale Allowance Overwrite - (File: rs/sns/governance/src/extensions.rs)

### Summary

The `approve_treasury_manager` function in SNS Governance calls `icrc2_approve` with `expected_allowance: None` for both the SNS token ledger and the ICP ledger. This means the approval unconditionally overwrites any existing allowance on the treasury manager canister, regardless of what the current allowance is. The specific amount to be approved is already known at call time (it is passed in as `sns_amount_e8s` / `icp_amount_e8s`), yet no `expected_allowance` guard is used to ensure the prior allowance is zero before setting the new one.

### Finding Description

In `rs/sns/governance/src/extensions.rs`, the `approve_treasury_manager` function is called in two contexts:

1. During `ValidatedRegisterExtension::execute` — when a new treasury manager extension is being registered.
2. During `execute_treasury_manager_deposit` — when a governance-approved deposit operation is executed.

In both cases, the function calls `icrc2_approve` with `expected_allowance: None`:

```rust
self.ledger
    .icrc2_approve(
        to,
        sns_amount_e8s,
        Some(expiry_time_nsec),
        self.transaction_fee_e8s_or_panic(),
        self.sns_treasury_subaccount(),
        None,  // <-- expected_allowance is None
    )
    .await
```

The comment in the code acknowledges this behavior:

> "If expected_allowance is None, the ledger *blindly* overwrites any existing allowance (even if non-zero). Therefore, there is no risk of double spending."

However, this reasoning is incomplete. While the ledger does overwrite the allowance atomically, the absence of an `expected_allowance` check means:

- If a prior allowance from a previous (possibly failed or partially-executed) deposit proposal is still active on the treasury manager, the new approval silently overwrites it without any indication that the prior allowance was non-zero.
- A malicious or buggy treasury manager canister that has not yet consumed its prior allowance will now have its allowance reset to the new amount — but the governance canister has no way to detect or react to this.
- More critically, between the `icrc2_approve` call and the subsequent `deposit` call on the treasury manager, the treasury manager could use the *old* allowance (if it was larger) or the new one, depending on timing and canister execution order. Since the approval is set without checking the prior state, the governance canister cannot guarantee the treasury manager starts from a clean slate. [1](#0-0) 

The specific amounts are known at call time — `sns_amount_e8s` and `icp_amount_e8s` are passed in directly from the validated proposal — so there is no reason not to set `expected_allowance: Some(0)` to ensure the prior allowance is zero before granting a new one. [2](#0-1) [3](#0-2) 

### Impact Explanation

**Governance authorization bug / ledger conservation bug.**

If a prior allowance from a previous deposit proposal is still active (e.g., the treasury manager canister did not consume it, or a prior deposit call failed after the approval was set), the new `icrc2_approve` call silently overwrites it. The treasury manager canister now holds a fresh allowance, but the governance canister has no record that the prior allowance was non-zero. The treasury manager could then call `icrc2_transfer_from` using the new allowance, effectively draining SNS treasury funds beyond what the current governance proposal authorized — since the prior unconsumed allowance is simply replaced rather than verified to be zero.

In the `execute_treasury_manager_deposit` flow, the sequence is:
1. `approve_treasury_manager(...)` — sets allowance to `N` (overwrites any prior allowance)
2. `call_canister(extension_canister_id, "deposit", ...)` — calls the treasury manager

If step 2 fails or the treasury manager is malicious, the allowance of `N` remains active on the ledger. A subsequent governance proposal that calls `approve_treasury_manager` again will overwrite it with a new `M`, but the treasury manager still holds the ability to drain up to `M` from the treasury — without governance being able to detect that the prior `N` was never consumed. [4](#0-3) 

### Likelihood Explanation

**Medium.** The treasury manager canister is a registered SNS extension, meaning it is installed by governance proposal. A malicious or buggy treasury manager that does not consume its allowance immediately, combined with a subsequent deposit proposal, creates the window for this issue. The attacker-controlled entry path is: submit a governance proposal to execute a deposit operation on a treasury manager that intentionally does not consume its allowance, then submit a second deposit proposal — the second `approve_treasury_manager` call will overwrite the first allowance without detecting it was non-zero, and the treasury manager can then drain the new allowance on top of the old one (if the old one was not yet consumed before the overwrite). This requires a malicious treasury manager canister to be registered, which itself requires a governance vote — but once registered, the attack is reachable by any unprivileged canister caller that controls the treasury manager.

### Recommendation

Pass `expected_allowance: Some(0)` in both `icrc2_approve` calls inside `approve_treasury_manager`. This ensures the ledger rejects the approval if a prior non-zero allowance exists, forcing governance to explicitly handle the case where a prior allowance was not consumed. If a prior allowance may legitimately be non-zero (e.g., due to a failed prior deposit), governance should first revoke it (set to 0) before granting a new one.

```rust
self.ledger
    .icrc2_approve(
        to,
        sns_amount_e8s,
        Some(expiry_time_nsec),
        self.transaction_fee_e8s_or_panic(),
        self.sns_treasury_subaccount(),
        Some(0),  // require prior allowance to be zero
    )
    .await
``` [5](#0-4) [6](#0-5) 

### Proof of Concept

1. A governance proposal registers a treasury manager extension canister (controlled by an attacker).
2. A governance proposal executes a deposit operation: `approve_treasury_manager` sets allowance = 100 SNS tokens on the treasury manager. The treasury manager's `deposit` method is called but intentionally does not consume the allowance (returns success without calling `icrc2_transfer_from`).
3. The allowance of 100 SNS tokens remains active on the SNS ledger for the treasury manager.
4. A second governance proposal executes another deposit operation: `approve_treasury_manager` is called again with `expected_allowance: None`, which overwrites the prior 100-token allowance with a new 50-token allowance — without detecting the prior 100 was unconsumed.
5. The treasury manager now calls `icrc2_transfer_from` for 50 SNS tokens (the new allowance). The prior 100-token allowance was silently discarded by the overwrite, but the treasury manager has already drained 50 tokens beyond what governance intended for this proposal cycle. [7](#0-6)

### Citations

**File:** rs/sns/governance/src/extensions.rs (L545-551)
```rust
                    governance
                        .approve_treasury_manager(
                            extension_canister_id,
                            treasury_allocation_sns_e8s,
                            treasury_allocation_icp_e8s,
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
