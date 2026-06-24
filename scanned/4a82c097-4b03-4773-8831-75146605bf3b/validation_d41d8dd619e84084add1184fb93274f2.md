### Title
SNS Governance `approve_treasury_manager` Blindly Overwrites Allowance Without CAS Guard, Enabling Double-Spend by Malicious Treasury Manager Canister - (File: rs/sns/governance/src/extensions.rs)

---

### Summary

`approve_treasury_manager` in SNS Governance calls `icrc2_approve` with `expected_allowance = None` for both the SNS token ledger and the ICP ledger. The inline comment incorrectly asserts this eliminates double-spend risk. In reality, because the IC's inter-canister calls are asynchronous, a malicious treasury manager canister can spend the existing allowance in the window between when governance dispatches the new `icrc2_approve` and when the ledger commits it, then spend the freshly set allowance as well — draining `old_allowance + new_allowance` instead of only `new_allowance`.

---

### Finding Description

`approve_treasury_manager` is invoked in two places:

1. **Initial registration** (`ValidatedRegisterExtension::execute`, line 545) — safe, because `ensure_no_code_is_installed` (line 523) guarantees the extension canister has no running code yet and cannot react.

2. **Subsequent deposit proposals** (`execute_treasury_manager_deposit`, line 1567) — **vulnerable**, because the treasury manager canister is already installed and fully operational.

The vulnerable code:

```rust
// rs/sns/governance/src/extensions.rs, lines 791-802
// If expected_allowance is None, the ledger *blindly* overwrites any existing
// allowance (even if non-zero). Therefore, there is no risk of double spending.

self.ledger
    .icrc2_approve(
        to,
        sns_amount_e8s,
        Some(expiry_time_nsec),
        self.transaction_fee_e8s_or_panic(),
        self.sns_treasury_subaccount(),
        None,   // <-- expected_allowance = None
    )
    .await
```

The comment's reasoning is inverted. `expected_allowance = None` means the ICRC-2 ledger skips the compare-and-swap check entirely and unconditionally replaces whatever allowance exists. This does not prevent the spender from consuming the old allowance before the replacement arrives; it only means the replacement will succeed regardless of the current state.

The ICRC-2 ledger's `approve` implementation confirms: when `expected_allowance` is `None`, the check at line 279 is bypassed and `set_allowance` is called unconditionally. [1](#0-0) 

The `icrc2_approve` call from governance to the ledger is an **asynchronous inter-canister call**. The ledger processes it in a future execution round. During the gap between dispatch and commit, the treasury manager canister — which is already running — can call `icrc2_transfer_from` against the governance treasury subaccount and consume the residual allowance from a prior deposit. [2](#0-1) [3](#0-2) 

---

### Impact Explanation

A malicious treasury manager canister can drain `R + B` tokens from the SNS governance treasury (both SNS-native tokens and ICP) instead of the governance-approved `B`, where `R` is the residual allowance from a previous deposit that was not fully consumed. Because both the SNS ledger and the ICP ledger approvals are issued without `expected_allowance`, both token pools are simultaneously at risk in every `ExecuteExtensionOperation` deposit proposal.

**Ledger conservation bug**: tokens leave the SNS/ICP treasury in excess of what governance voted to allocate. [4](#0-3) [5](#0-4) 

---

### Likelihood Explanation

- The treasury manager canister is a **canister caller** — an explicitly in-scope attacker role.
- It is already installed and operational when `execute_treasury_manager_deposit` runs; no privileged key or majority corruption is needed.
- The canister can observe SNS governance proposals on-chain (they are public) and know exactly when a new deposit approval is incoming.
- Exploiting the window requires only a single `icrc2_transfer_from` call timed before the governance `icrc2_approve` is committed — a straightforward inter-canister race on the IC.
- The NNS community reviews treasury manager implementations, but a subtly malicious implementation could hide this logic. The protocol-level fix must not rely on off-chain review. [6](#0-5) 

---

### Recommendation

Pass the current on-chain allowance as `expected_allowance` when calling `icrc2_approve`. Before issuing the approval, query the ledger for the current allowance and supply it as the CAS guard:

```rust
let current_sns_allowance = self.ledger
    .icrc2_allowance(sns_treasury_account, treasury_manager_account)
    .await?
    .amount;

self.ledger
    .icrc2_approve(
        to,
        sns_amount_e8s,
        Some(expiry_time_nsec),
        self.transaction_fee_e8s_or_panic(),
        self.sns_treasury_subaccount(),
        Some(current_sns_allowance),  // CAS guard
    )
    .await?;
```

If the treasury manager front-runs and spends the old allowance, the `expected_allowance` check will fail with `AllowanceChanged`, causing the governance proposal execution to abort rather than silently granting a second allowance. The same fix must be applied to the ICP ledger approval. [7](#0-6) 

---

### Proof of Concept

**Setup**: An SNS has a treasury manager extension already installed. A prior deposit proposal set an allowance of `A = 500_000_000` SNS tokens. The treasury manager consumed only `300_000_000`, leaving a residual allowance of `R = 200_000_000`.

**Attack**:

1. A new SNS governance proposal (`ExecuteExtensionOperation` / deposit) passes, authorizing `B = 1_000_000_000` SNS tokens.

2. `execute_treasury_manager_deposit` calls `approve_treasury_manager(extension_canister_id, 1_000_000_000, ...)`.

3. Governance dispatches `icrc2_approve(treasury_manager, 1_000_000_000, None)` to the SNS ledger (async call, not yet committed).

4. The malicious treasury manager canister — which monitors governance proposals — immediately calls `icrc2_transfer_from(governance_sns_treasury, treasury_manager, 200_000_000)`, consuming the residual allowance `R` before the ledger commits the new approval.

5. The ledger commits the new `icrc2_approve`, setting allowance to `B = 1_000_000_000`.

6. The treasury manager calls `icrc2_transfer_from(governance_sns_treasury, treasury_manager, 1_000_000_000)`, consuming `B`.

7. **Total drained**: `200_000_000 + 1_000_000_000 = 1_200_000_000` SNS tokens. Governance only authorized `1_000_000_000`.

The same sequence applies to the ICP ledger approval issued immediately after. [2](#0-1) [8](#0-7) [9](#0-8)

### Citations

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L232-241)
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
