### Title
Allowance Front-Running in `approve_treasury_manager` Enables Treasury Manager to Double-Spend SNS and ICP Treasury Funds - (File: `rs/sns/governance/src/extensions.rs`)

### Summary

`Governance::approve_treasury_manager` calls `icrc2_approve` with `expected_allowance: None` for both the SNS token ledger and the ICP ledger when granting a treasury manager extension canister spending rights. Because `expected_allowance` is omitted, the ledger blindly overwrites any existing allowance. A malicious or compromised treasury manager canister can observe a pending governance proposal (proposals are public during their voting period), drain the **old** allowance via `icrc2_transfer_from` before the proposal executes, and then drain the **new** allowance after it is set — spending both.

### Finding Description

`approve_treasury_manager` is called in two places:

1. `ValidatedRegisterExtension::execute` — when a new treasury manager extension is first registered.
2. `execute_treasury_manager_deposit` — when a subsequent "deposit" governance proposal is executed.

In both cases the function issues two `icrc2_approve` calls with `expected_allowance: None`:

```rust
// rs/sns/governance/src/extensions.rs lines 791–820
// If expected_allowance is None, the ledger *blindly* overwrites any existing
// allowance (even if non-zero). Therefore, there is no risk of double spending.

self.ledger
    .icrc2_approve(to, sns_amount_e8s, Some(expiry_time_nsec),
                   self.transaction_fee_e8s_or_panic(),
                   self.sns_treasury_subaccount(),
                   None,   // ← expected_allowance omitted
    ).await ...

self.nns_ledger
    .icrc2_approve(to, icp_amount_e8s, Some(expiry_time_nsec),
                   icp_ledger::DEFAULT_TRANSFER_FEE.get_e8s(),
                   self.icp_treasury_subaccount(),
                   None,   // ← expected_allowance omitted
    ).await ...
```

The inline comment claims "there is no risk of double spending." This is incorrect. The ICRC-2 standard's `expected_allowance` field exists precisely to prevent a spender from exploiting the window between an old allowance being replaced and a new one being set. The ledger's `approve` implementation confirms that `None` means the old allowance is unconditionally overwritten:

```rust
// rs/ledger_suite/common/ledger_core/src/approvals.rs lines 278–319
Some(old_allowance) => {
    if let Some(expected_allowance) = expected_allowance { // only checked if Some
        ...
    }
    // unconditional overwrite follows
    table.allowances_data.set_allowance(key.clone(), Allowance { amount, ... });
```

### Impact Explanation

A treasury manager canister that has already been granted an allowance (e.g., from a prior `RegisterExtension` or `ExecuteExtensionOperation` proposal) can:

1. Monitor the public proposal queue and detect a new "deposit" proposal that will call `approve_treasury_manager` with a new allowance Y.
2. Before the proposal executes, call `icrc2_transfer_from` to drain the **existing** allowance X from the SNS treasury subaccount and/or the ICP treasury account.
3. After the proposal executes and the new allowance Y is set, drain Y as well.

Total loss: **X + Y** tokens instead of the intended Y. Both SNS-native tokens and ICP are at risk. The SNS treasury is the DAO's primary on-chain asset pool, so the impact is direct, irreversible loss of DAO funds.

### Likelihood Explanation

- Governance proposals on the IC are public and have a mandatory voting period (typically days). Any observer — including the treasury manager canister itself or its operator — can see the pending allowance change well in advance of execution.
- The treasury manager is a third-party extension canister. While it must pass a WASM hash check against an allowlist, a compromised or malicious operator can trigger `icrc2_transfer_from` at any time without any governance approval.
- The attack requires no privileged access, no key compromise, and no subnet-majority corruption. It only requires the treasury manager canister to call a standard ICRC-2 ledger method it is already authorized to call.
- The `execute_treasury_manager_deposit` flow (line 1566–1573) explicitly calls `approve_treasury_manager` **before** calling `deposit` on the extension, meaning the allowance is live and drainable in the inter-canister call gap.

### Recommendation

Pass the current on-chain allowance as `expected_allowance` when calling `icrc2_approve`. This makes the approval atomic with respect to the current state: if the treasury manager has already spent (or front-run) the old allowance, the ledger will return `AllowanceChanged` and the governance proposal will fail safely rather than silently granting a fresh allowance on top of a drained one.

```rust
// Query the current allowance first
let current_sns_allowance = self.ledger
    .icrc2_allowance(from_account, to)
    .await?
    .allowance;

self.ledger.icrc2_approve(
    to,
    sns_amount_e8s,
    Some(expiry_time_nsec),
    self.transaction_fee_e8s_or_panic(),
    self.sns_treasury_subaccount(),
    Some(current_sns_allowance),  // ← pass expected_allowance
).await ...
```

Alternatively, explicitly zero out the existing allowance before setting the new one (two-step approve: set to 0, then set to the new amount), mirroring the ERC-20 safe-approve pattern.

### Proof of Concept

1. SNS DAO passes `RegisterExtension` proposal → `approve_treasury_manager` sets SNS allowance = 500 SNS, ICP allowance = 10 ICP for `treasury_manager_canister`.
2. SNS DAO passes a second `ExecuteExtensionOperation { operation_name: "deposit", ... }` proposal with new amounts 300 SNS / 5 ICP. This proposal is visible on-chain during its voting period.
3. Treasury manager operator observes the pending proposal and calls `icrc2_transfer_from(from=sns_treasury_subaccount, to=attacker_account, amount=500)` on the SNS ledger and `icrc2_transfer_from(from=icp_treasury, to=attacker_account, amount=10)` on the ICP ledger — draining the full existing allowances.
4. The deposit proposal passes and executes: `approve_treasury_manager` sets SNS allowance = 300, ICP allowance = 5 (blindly, because `expected_allowance: None`).
5. Treasury manager drains the new allowances: 300 SNS + 5 ICP.
6. Total stolen: **800 SNS + 15 ICP** instead of the intended 300 SNS + 5 ICP. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L232-242)
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
```

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L278-320)
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

                    if expires_at != old_allowance.expires_at {
                        if let Some(old_expiration) = old_allowance.expires_at {
                            table
                                .allowances_data
                                .remove_expiry(old_expiration, key.clone());
                        }
                        if let Some(expires_at) = expires_at {
                            table.allowances_data.insert_expiry(expires_at, key);
                        }
                    }
                    Ok(amount)
                }
```
