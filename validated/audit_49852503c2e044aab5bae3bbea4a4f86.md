### Title
SNS TreasuryManager Treasury Funds Locked When `SNS_EXTENSIONS_ENABLED` Flag Disables Withdraw Operations - (File: `rs/sns/governance/src/governance.rs`)

### Summary

The `perform_execute_extension_operation` function in SNS Governance applies the `SNS_EXTENSIONS_ENABLED` feature-flag check uniformly to **all** extension operations — including the `withdraw` operation that recovers treasury funds from a TreasuryManager extension. If this flag is ever set to `false` (e.g., via a future SNS Governance canister upgrade that changes the compile-time default), funds already deposited into TreasuryManager extension canisters and forwarded to an external custodian (e.g., a DEX) would be permanently locked with no fallback recovery path. This is the direct IC analog of the Kiosk `uid_mut` / `allow_extensions = false` fund-locking pattern.

---

### Finding Description

**Root cause — `rs/sns/governance/src/lib.rs`, lines 27–34:**

```rust
thread_local! {
    static SNS_EXTENSIONS_ENABLED: Cell<bool> = const { Cell::new(true) };
}

pub fn is_sns_extensions_enabled() -> bool {
    SNS_EXTENSIONS_ENABLED.get()
}
```

`SNS_EXTENSIONS_ENABLED` is a `thread_local` `Cell<bool>` that is **not persisted in stable memory**. It resets to its compile-time default on every canister start or upgrade. The CHANGELOG confirms this flag was previously `false` before Proposal 138584 (2025-09-19) turned it on. [1](#0-0) 

**Uniform gate — `rs/sns/governance/src/governance.rs`, lines 2558–2568:**

```rust
async fn perform_execute_extension_operation(
    &self,
    execute_extension_operation: ExecuteExtensionOperation,
) -> Result<(), GovernanceError> {
    // Check if SNS extensions are enabled
    if !crate::is_sns_extensions_enabled() {
        return Err(GovernanceError::new_with_message(
            ErrorType::PreconditionFailed,
            "SNS extensions are not enabled",
        ));
    }
    ...
}
```

The same flag also gates `perform_register_extension` and `perform_upgrade_extension`. There is **no carve-out** for the `withdraw` operation. [2](#0-1) 

**Deposit flow creates irreversible off-canister exposure:**

`execute_treasury_manager_deposit` first calls `approve_treasury_manager` (ICRC-2 approve on both SNS and ICP ledgers), then calls `deposit` on the extension canister. The extension canister pulls the approved funds via `transfer_from` and forwards them to an **external custodian** (e.g., a DEX liquidity pool). [3](#0-2) 

**Withdraw path — the only recovery route:**

`execute_treasury_manager_withdraw` calls `withdraw` on the extension canister, which instructs the external custodian to return funds to the SNS treasury owner account. [4](#0-3) 

If `SNS_EXTENSIONS_ENABLED` is `false`, `perform_execute_extension_operation` returns early before reaching `execute_treasury_manager_withdraw`. The funds remain in the external custodian with no protocol-level recovery path.

**Registered extensions survive upgrades (stable memory):**

`REGISTERED_EXTENSIONS` is backed by `ic_stable_structures::BTreeMap` and persists across upgrades. So extensions remain registered even after an upgrade that resets `SNS_EXTENSIONS_ENABLED` to `false`, but their `withdraw` operation becomes unreachable. [5](#0-4) 

---

### Impact Explanation

**Vulnerability class:** Ledger conservation bug / treasury fund locking.

If `SNS_EXTENSIONS_ENABLED` is set to `false` in a future SNS Governance upgrade (e.g., as a security response to a discovered extension vulnerability), all SNS treasury funds already deposited into TreasuryManager extension canisters and forwarded to external custodians become permanently inaccessible. The `withdraw` proposal type is blocked by the same flag that blocks `deposit` and `register`, with no emergency fallback. The SNS DAO has no protocol-level mechanism to recover those funds.

---

### Likelihood Explanation

**Medium-Low.** The flag is currently hardcoded to `true` and is not runtime-configurable. Triggering the lock requires an NNS governance upgrade of SNS Governance that changes the compile-time default to `false`. This is a realistic scenario: the CHANGELOG shows the flag was `false` before Proposal 138584, and DFINITY has historically used such flags to gate or roll back features. A security incident affecting the extension system could motivate such a rollback, inadvertently locking treasury funds. There is no malicious intent required — the governance majority could be acting in good faith. [6](#0-5) 

---

### Recommendation

Separate the `SNS_EXTENSIONS_ENABLED` gate by operation type. Specifically, the `withdraw` (fund-recovery) operation should **not** be gated by the extensions-enabled flag. A safe pattern is:

```rust
async fn perform_execute_extension_operation(...) -> Result<(), GovernanceError> {
    let is_withdraw = /* inspect operation_name */;
    if !crate::is_sns_extensions_enabled() && !is_withdraw {
        return Err(...);
    }
    ...
}
```

Alternatively, introduce a dedicated `perform_emergency_withdraw_extension` path that bypasses the feature flag and is always available as long as the extension is registered in stable memory. This mirrors the remediation recommended in the original Kiosk report: a fallback mechanism that enables termination/withdrawal of extensions regardless of the global enable flag.

---

### Proof of Concept

1. SNS DAO passes a `RegisterExtension` proposal, depositing SNS tokens and ICP into a TreasuryManager extension canister (e.g., KongSwap). The extension forwards those funds to an external DEX. [7](#0-6) 

2. A security issue is discovered in the extension system. DFINITY submits an NNS proposal to upgrade all SNS Governance canisters with `SNS_EXTENSIONS_ENABLED` defaulting to `false`.

3. After the upgrade, `is_sns_extensions_enabled()` returns `false`. [8](#0-7) 

4. The SNS DAO submits an `ExecuteExtensionOperation` proposal with `operation_name = "withdraw"`. Execution reaches `perform_execute_extension_operation`, which returns `Err("SNS extensions are not enabled")` before calling `execute_treasury_manager_withdraw`. [9](#0-8) 

5. The funds remain in the external custodian. The `REGISTERED_EXTENSIONS` stable-memory map still contains the extension entry, but the withdraw path is permanently blocked. No other IC protocol mechanism exists to recover the funds. [10](#0-9)

### Citations

**File:** rs/sns/governance/src/lib.rs (L27-34)
```rust
// Feature flag for SNS Extensions
thread_local! {
    static SNS_EXTENSIONS_ENABLED: Cell<bool> = const { Cell::new(true) };
}

pub fn is_sns_extensions_enabled() -> bool {
    SNS_EXTENSIONS_ENABLED.get()
}
```

**File:** rs/sns/governance/src/governance.rs (L2558-2568)
```rust
    async fn perform_execute_extension_operation(
        &self,
        execute_extension_operation: ExecuteExtensionOperation,
    ) -> Result<(), GovernanceError> {
        // Check if SNS extensions are enabled
        if !crate::is_sns_extensions_enabled() {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "SNS extensions are not enabled",
            ));
        }
```

**File:** rs/sns/governance/src/extensions.rs (L505-594)
```rust
impl ValidatedRegisterExtension {
    pub async fn execute(self, governance: &Governance) -> Result<(), GovernanceError> {
        let main = async || {
            let context = governance.treasury_manager_deposit_context().await?;

            let ValidatedRegisterExtension {
                spec,
                init,
                extension_canister_id,
                wasm,
            } = self;

            governance
                .register_extension_with_root(extension_canister_id)
                .await?;

            // Before granting any SNS capabilities to the extension, we must ensure that old code
            // could not have snuck in between proposal (re-)validation and the SNS assuming control.
            governance
                .ensure_no_code_is_installed(extension_canister_id)
                .await?;

            // This needs to happen before the canister code is installed.
            let init_blob = match init {
                ValidatedExtensionInit::TreasuryManager(ValidatedDepositOperationArg {
                    treasury_allocation_sns_e8s,
                    treasury_allocation_icp_e8s,
                    original,
                }) => {
                    let init_blob =
                        construct_treasury_manager_init_payload(context.clone(), original)
                            .map_err(|err| {
                                GovernanceError::new_with_message(
                                    ErrorType::InvalidProposal,
                                    format!(
                                        "Error constructing TreasuryManagerInit payload: {err}"
                                    ),
                                )
                            })?;

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

**File:** rs/sns/governance/src/storage.rs (L26-28)
```rust
    pub static REGISTERED_EXTENSIONS: RefCell<BTreeMap<Principal, ExtensionSpec, VM>> = MEMORY_MANAGER.with_borrow(|memory_manager| {
        RefCell::new(BTreeMap::init(memory_manager.get(REGISTERED_EXTENSIONS_MEMORY_ID)))
    });
```

**File:** rs/sns/governance/src/storage.rs (L35-45)
```rust
pub fn cache_registered_extension(canister_id: CanisterId, spec: ExtensionSpec) {
    REGISTERED_EXTENSIONS.with_borrow_mut(|map| map.insert(canister_id.get().0, spec));
}

pub fn clear_registered_extension_cache(canister_id: CanisterId) {
    REGISTERED_EXTENSIONS.with_borrow_mut(|map| map.remove(&canister_id.get().0));
}

pub fn get_registered_extension_from_cache(canister_id: CanisterId) -> Option<ExtensionSpec> {
    REGISTERED_EXTENSIONS.with_borrow(|map| map.get(&canister_id.get().0))
}
```

**File:** rs/sns/governance/CHANGELOG.md (L55-61)
```markdown
# 2025-09-19: Proposal 138584

https://dashboard.internetcomputer.org/proposal/138584

## Added

* The feature flag `SNS_EXTENSIONS_ENABLED` is turned on. Enabling it allows for deployment of SNS extensions.
```
