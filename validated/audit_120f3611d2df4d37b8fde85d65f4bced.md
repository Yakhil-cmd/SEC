### Title
`UpgradeExtension` Does Not Withdraw Funds Before Upgrading TreasuryManager Extension - (File: `rs/sns/governance/src/extensions.rs`)

### Summary
The `ValidatedUpgradeExtension::execute` function upgrades a TreasuryManager extension canister directly via `CanisterInstallMode::Upgrade` without first calling `withdraw` on the old extension. This is the direct IC analog of the Sherlock `updateYieldStrategy` bug: SNS treasury funds already deposited into the extension (and further deployed to external custodians such as DEX liquidity pools) are not recalled before the upgrade, leaving them potentially inaccessible if the new WASM version has incompatible state tracking.

### Finding Description
The `UpgradeExtension` governance action is executed via `perform_upgrade_extension` → `ValidatedUpgradeExtension::execute`:

```rust
// rs/sns/governance/src/extensions.rs:1208-1241
impl ValidatedUpgradeExtension {
    pub async fn execute(self, governance: &Governance) -> Result<(), GovernanceError> {
        ...
        let arg_bytes = match &upgrade_arg {
            ValidatedExtensionUpgradeArg::TreasuryManager => {
                construct_treasury_manager_upgrade_payload()...
            }
        };

        governance
            .upgrade_non_root_canister(
                extension_canister_id,
                wasm,
                arg_bytes,
                CanisterInstallMode::Upgrade,  // <-- direct upgrade, no withdraw first
            )
            .await?;

        cache_registered_extension(extension_canister_id, spec);
        Ok(())
    }
}
``` [1](#0-0) 

The upgrade payload is an empty `TreasuryManagerUpgrade {}` struct with no mechanism to trigger fund withdrawal:

```rust
fn construct_treasury_manager_upgrade_payload() -> Result<Vec<u8>, String> {
    let arg = TreasuryManagerArg::Upgrade(TreasuryManagerUpgrade {});
    ...
}
``` [2](#0-1) 

The TreasuryManager extension holds SNS treasury funds (ICP and SNS tokens) that flow from the SNS governance treasury → TreasuryManager → external custodian (e.g., KongSwap DEX liquidity pool). The `withdraw` operation exists and is callable via `ExecuteExtensionOperation`: [3](#0-2) 

The `TreasuryManager` trait defines `withdraw` as a standard operation: [4](#0-3) 

The asset flow documented in the TreasuryManager DID confirms funds can be held in `external_custodian` (DEX) at the time of an upgrade: [5](#0-4) 

The `BalanceBook` invariant shows that `managed_assets = treasury_manager + treasury_owner + external_custodian`, meaning funds in the DEX are part of the managed pool but are not under the extension canister's direct custody: [6](#0-5) 

### Impact Explanation
When `UpgradeExtension` executes:
1. The extension canister is stopped by the IC upgrade process.
2. Funds held in external custodians (DEX pools) remain there — they are not recalled.
3. The new WASM's `post_upgrade` receives an empty `TreasuryManagerUpgrade {}` with no information about existing positions.
4. If the new WASM version uses incompatible stable-memory data structures to track DEX positions (e.g., a different serialization format for LP token records), the new code cannot locate or withdraw those funds.
5. SNS treasury funds (ICP and SNS tokens) become permanently inaccessible — there is no recovery path because the old WASM is gone and the new WASM cannot interpret the old position records.

Even in the non-permanent case, during the upgrade window the extension is stopped and cannot respond to `withdraw` calls, creating a temporary denial-of-service on treasury funds.

### Likelihood Explanation
The `UpgradeExtension` proposal is a standard SNS governance action reachable by any SNS token holder who can submit a proposal. The WASM must be in the `ALLOWED_EXTENSIONS` allowlist, but allowlisted WASMs can still introduce breaking state-migration changes between versions. The `validate_upgrade_extension` function only checks that the new version number is higher and the name matches — it does not verify backward compatibility of stable memory: [7](#0-6) 

Any SNS that has deposited treasury funds into a TreasuryManager extension and subsequently upgrades that extension is exposed to this risk. The likelihood is **medium**: it requires a governance proposal to pass, but the SNS governance is expected to upgrade extensions over time, and the bug is triggered by any upgrade where the new WASM cannot interpret the old DEX position state.

### Recommendation
Before calling `upgrade_non_root_canister`, `ValidatedUpgradeExtension::execute` should call `withdraw` on the old extension to pull all funds back to the SNS treasury. After the upgrade completes successfully, the new extension can re-deposit funds via a subsequent `ExecuteExtensionOperation(deposit)` proposal. This mirrors the short-term fix recommended in the Sherlock report: call `withdrawAll()` before switching strategies.

### Proof of Concept
1. SNS governance approves a `RegisterExtension` proposal, depositing ICP and SNS tokens into a TreasuryManager extension (e.g., KongSwap adaptor). The extension deploys those funds into a DEX liquidity pool. At this point `external_custodian > 0` in the `BalanceBook`.
2. A new version of the KongSwap adaptor WASM is added to `ALLOWED_EXTENSIONS` with a higher version number. The new version changes the stable-memory layout for tracking LP positions (e.g., migrates from a `BTreeMap<(Principal, u64), LpPosition>` to a `BTreeMap<String, LpPosition>`).
3. SNS governance approves an `UpgradeExtension` proposal targeting the extension canister.
4. `perform_upgrade_extension` → `ValidatedUpgradeExtension::execute` is called. No `withdraw` is called. `upgrade_non_root_canister` stops the canister, installs the new WASM, and calls `post_upgrade` with an empty `TreasuryManagerUpgrade {}`.
5. The new WASM's `post_upgrade` cannot deserialize the old LP position records from stable memory. The extension starts up with an empty position map.
6. The DEX still holds the liquidity, but the extension has no record of it. Calling `withdraw` on the new extension returns zero funds. The SNS treasury has permanently lost the deposited ICP and SNS tokens.

### Citations

**File:** rs/sns/governance/src/extensions.rs (L1081-1085)
```rust
fn construct_treasury_manager_upgrade_payload() -> Result<Vec<u8>, String> {
    let arg = TreasuryManagerArg::Upgrade(TreasuryManagerUpgrade {});

    candid::encode_one(&arg).map_err(|err| format!("Error encoding TreasuryManagerArg: {err}"))
}
```

**File:** rs/sns/governance/src/extensions.rs (L1208-1241)
```rust
impl ValidatedUpgradeExtension {
    pub async fn execute(self, governance: &Governance) -> Result<(), GovernanceError> {
        let ValidatedUpgradeExtension {
            extension_canister_id,
            wasm,
            upgrade_arg,
            spec,
            ..
        } = self;

        let arg_bytes = match &upgrade_arg {
            ValidatedExtensionUpgradeArg::TreasuryManager => {
                construct_treasury_manager_upgrade_payload().map_err(|err| {
                    // This should not be possible, and it's not clear that it falls in another category of error.
                    GovernanceError::new_with_message(ErrorType::Unspecified, err)
                })?
            }
        };

        governance
            .upgrade_non_root_canister(
                extension_canister_id,
                wasm,
                arg_bytes,
                CanisterInstallMode::Upgrade,
            )
            .await?;

        // Update the extension cache with the new spec
        cache_registered_extension(extension_canister_id, spec);

        Ok(())
    }
}
```

**File:** rs/sns/governance/src/extensions.rs (L1297-1303)
```rust
    // Check that the new version is higher than the current version
    if new_spec.version <= current_extension.version {
        return Err(format!(
            "New extension version {} must be higher than current version {}",
            new_spec.version.0, current_extension.version.0
        ));
    }
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

**File:** rs/sns/treasury_manager/src/lib.rs (L257-261)
```rust
    /// Implements the `withdraw` API function.
    fn withdraw(
        &mut self,
        request: WithdrawRequest,
    ) -> impl std::future::Future<Output = TreasuryManagerResult> + Send;
```

**File:** rs/sns/treasury_manager/treasury_manager.did (L143-172)
```text
/// Let `k` denote a particular state, `party[k]` denote the account balance of `party`
/// in state `k`, and `managed_assets` be the sum of all assets managed on behalf of
/// the treasury owner in state `k`.
///
/// Initial managed assets
/// ----------------------
/// managed_assets[0] == treasury_manager[0]
///
///     (treasury_owner[0] == external_custodian[0] == fee_collector[0]
///      == payees[0] == payers[0] == suspense[0] == 0)
///
/// Current managed assets
/// ----------------------
/// managed_assets[k] == treasury_manager[k] + treasury_owner[k] + external_custodian[k]
///
/// Under "normal operations", the following invariants hold for all k > 0:
/// 1) suspense[k] == 0
/// 2) managed_assets[k] == managed_assets[k-1] + payers[k] - payees[k] - fee_collector[k]
type BalanceBook = record {
  treasury_owner : opt Balance;
  treasury_manager : opt Balance;
  external_custodian : opt Balance;
  fee_collector : opt Balance;
  payees : opt Balance;
  payers : opt Balance;

  // An account in which items are entered temporarily before allocation to the correct
  // or final account, e.g., due to transient errors.
  suspense : opt Balance;
};
```

**File:** rs/sns/treasury_manager/treasury_manager.did (L279-295)
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
```
