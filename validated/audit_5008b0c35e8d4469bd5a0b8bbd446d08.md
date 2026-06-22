### Title
SNS Treasury Funds Permanently Bricked if External Custodian Canister (KongSwap) Becomes Unavailable - (File: rs/sns/treasury_manager/src/lib.rs)

### Summary
The `TreasuryManager` trait mandates `deposit` and `withdraw` operations but defines no emergency-withdraw path. When SNS treasury funds are deposited into an extension canister (e.g., the KongSwap adaptor) and that extension canister in turn deposits them into an external custodian canister (KongSwap DEX), any permanent unavailability of the external custodian causes every subsequent `withdraw` governance proposal to fail. Because the `TreasuryManager` interface has no bypass path, and because the SNS governance's `execute_treasury_manager_withdraw` unconditionally calls `extension_canister_id.withdraw()`, the treasury funds are bricked until the extension canister is upgraded via a separate governance proposal.

### Finding Description
The `TreasuryManager` trait is defined in `rs/sns/treasury_manager/src/lib.rs`:

```rust
pub trait TreasuryManager {
    fn deposit(&mut self, request: DepositRequest) -> impl Future<Output = TreasuryManagerResult> + Send;
    fn withdraw(&mut self, request: WithdrawRequest) -> impl Future<Output = TreasuryManagerResult> + Send;
    fn audit_trail(&self, request: AuditTrailRequest) -> AuditTrail;
    fn balances(&self, request: BalancesRequest) -> TreasuryManagerResult;
    fn refresh_balances(&mut self) -> impl Future<Output = ()> + Send;
    fn issue_rewards(&mut self) -> impl Future<Output = ()> + Send;
}
```

No emergency-withdraw variant exists. The withdrawal flow in `rs/sns/governance/src/extensions.rs` is:

```rust
async fn execute_treasury_manager_withdraw(
    governance: &Governance,
    extension_canister_id: CanisterId,
    arg: ValidatedWithdrawOperationArg,
) -> Result<(), GovernanceError> {
    ...
    let balances = governance
        .env
        .call_canister(extension_canister_id, "withdraw", arg_blob)
        .await
        ...?;
    ...
}
```

The call to `extension_canister_id.withdraw()` is unconditional. The extension canister's `withdraw` implementation must call the external custodian (KongSwap DEX) to retrieve liquidity before transferring tokens back to the SNS governance treasury. If the external custodian canister is stopped, deleted, or permanently out of cycles, every call to `withdraw` will trap or return an error, causing the SNS governance proposal to fail with no alternative code path.

The deposit flow in `execute_treasury_manager_deposit` first calls `approve_treasury_manager` (granting a 1-hour ICRC-2 allowance) and then calls `extension_canister_id.deposit()`. Once funds are transferred into the external custodian, the only defined protocol path to retrieve them is through `withdraw`, which requires the external custodian to be responsive.

The `TreasuryManager` DID interface confirms only `deposit`, `withdraw`, `balances`, and `audit_trail` are exposed — no emergency or bypass function exists.

### Impact Explanation
SNS treasury assets (SNS tokens and ICP) deposited into the KongSwap adaptor extension canister and forwarded to KongSwap DEX become permanently inaccessible if KongSwap DEX is stopped or deleted. Every `ExecuteExtensionOperation { operation_name: "withdraw" }` governance proposal will fail. The `validate_execute_extension_operation` function only recognises operations registered in the extension spec (`deposit` and `withdraw`), so no alternative operation name can be submitted. The only recovery path — upgrading the extension canister via `UpgradeExtension` — requires a separate governance proposal, a supermajority vote, and correct new code to be written and audited under time pressure, all while treasury funds remain frozen.

### Likelihood Explanation
KongSwap is an external canister (`2ipq2-uqaaa-aaaar-qailq-cai`) not controlled by the SNS or DFINITY. It can become unavailable through: (1) the KongSwap team stopping or deleting the canister, (2) the canister running out of cycles (a realistic operational risk for any third-party canister), or (3) a catastrophic bug causing the canister to trap on all calls. The MODULE.bazel comment already notes "The kong repository disappeared" when referencing the KongSwap WASM, indicating the project's continuity is not guaranteed. Any SNS that has deposited treasury funds into the KongSwap adaptor is exposed.

### Recommendation
Add an `emergency_withdraw` function to the `TreasuryManager` trait (and its DID interface) that transfers any tokens held directly by the extension canister back to the treasury owner without calling the external custodian. The SNS governance's `execute_treasury_manager_withdraw` (or a new `execute_treasury_manager_emergency_withdraw`) should call this function when the normal `withdraw` path fails. This mirrors the fix applied to the analogous LpWrapper bug, where the attempt to interact with the unavailable gauge was skipped in the emergency path.

### Proof of Concept

1. SNS governance passes a `RegisterExtension` proposal, deploying the KongSwap adaptor and depositing `N` SNS tokens + `M` ICP into it via `approve_treasury_manager` + `extension_canister_id.deposit()`. Funds flow: SNS treasury → extension canister → KongSwap DEX.

2. KongSwap DEX canister (`2ipq2-uqaaa-aaaar-qailq-cai`) runs out of cycles and is frozen, or is stopped by its controllers.

3. SNS governance passes an `ExecuteExtensionOperation { operation_name: "withdraw" }` proposal.

4. `execute_treasury_manager_withdraw` in `rs/sns/governance/src/extensions.rs` calls:
   ```rust
   governance.env.call_canister(extension_canister_id, "withdraw", arg_blob).await
   ```

5. The extension canister's `withdraw` implementation calls KongSwap DEX to remove liquidity. The call fails (canister stopped/frozen).

6. The governance proposal is marked as failed. SNS treasury funds remain locked in KongSwap DEX.

7. No `emergency_withdraw` operation exists in the `TreasuryManager` trait or the SNS governance's `ExecuteExtensionOperation` handler. The only recovery is an `UpgradeExtension` proposal with custom bypass code — requiring a new governance cycle, supermajority, and correct implementation under duress. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/sns/treasury_manager/src/lib.rs (L250-282)
```rust
pub trait TreasuryManager {
    /// Implements the `deposit` API function.
    fn deposit(
        &mut self,
        request: DepositRequest,
    ) -> impl std::future::Future<Output = TreasuryManagerResult> + Send;

    /// Implements the `withdraw` API function.
    fn withdraw(
        &mut self,
        request: WithdrawRequest,
    ) -> impl std::future::Future<Output = TreasuryManagerResult> + Send;

    /// Implements the `audit_trail` API query function.
    fn audit_trail(&self, request: AuditTrailRequest) -> AuditTrail;

    /// Implements the `balances` API query function.
    fn balances(&self, request: BalancesRequest) -> TreasuryManagerResult;

    // While the following methods go beyond just the Treasury Manager API agreement, they guide
    // the implementers to organize the code in a reasonable and predictable way.

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

**File:** rs/sns/governance/src/extensions.rs (L1612-1660)
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
```

**File:** rs/sns/treasury_manager/treasury_manager.did (L279-301)
```text
// Expects flow of assets:
//
// (A) Initialization / Deposit
// ============================
//                                      ,--------------> payees
//                                     /
// treasury_owner ---> treasury_manager ---> external_custodian
//              \                      \                       \
//               `----------------------`-----------------------`--------> fee_collector
//
// (B) Withdrawal
// ==============
//             payers --->.
//                         \
//  external_custodian ---> treasury_manager ---> treasury_owner
//                    \                     \
//                     `---------------------`---------------------------> fee_collector
service : (TreasuryManagerArg) -> {
  deposit : (DepositRequest) -> (Result);
  withdraw : (WithdrawRequest) -> (Result);
  balances : (record {}) -> (Result) query;
  audit_trail : (record {}) -> (AuditTrail) query;
}
```
