### Title
SNS Extension Cannot Be Deregistered After Successful Registration - (`rs/sns/governance/src/governance.rs`, `rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto`)

### Summary

The SNS governance system provides a `RegisterExtension` proposal action (Id = 17) to register an SNS extension canister, but provides no corresponding `DeregisterExtension` governance proposal action. Once an extension is successfully registered, the SNS DAO has no on-chain governance mechanism to remove it. This is a direct analog to M-11: an entity can be added to a privileged registry but never removed.

### Finding Description

The SNS `Action` enum defines three extension-related proposal types:

- `RegisterExtension` (Id = 17) — registers an extension canister
- `ExecuteExtensionOperation` (Id = 18) — calls an operation on a registered extension
- `UpgradeExtension` (Id = 19) — upgrades a registered extension's WASM [1](#0-0) 

There is no `DeregisterExtension` action. The `perform_action` dispatch in `rs/sns/governance/src/governance.rs` handles `RegisterExtension` and `UpgradeExtension` but has no branch for deregistration of a successfully registered extension. [2](#0-1) 

On the Root canister side, `register_extension` appends the extension's canister ID to `state.extensions.extension_canister_ids` permanently: [3](#0-2) 

The only removal path that exists is `clean_up_failed_register_extension`, which is called internally only when a `RegisterExtension` proposal execution fails mid-flight. It is not exposed as a governance proposal action and is not reachable for a successfully registered extension. [4](#0-3) 

The governance-side extension cache (`REGISTERED_EXTENSIONS` in stable memory) has a `clear_registered_extension_cache` function, but it is never called from any governance proposal path: [5](#0-4) 

### Impact Explanation

SNS extensions are a privileged class of canisters. Unlike regular dapp canisters, extensions are co-controlled by both the SNS Root and SNS Governance canisters, and are granted treasury allowances (SNS tokens and ICP) via `approve_treasury_manager` during registration: [6](#0-5) 

If a registered extension is found to be vulnerable or actively exploited, the SNS DAO has no governance proposal mechanism to:
1. Remove the extension from the Root's `extension_canister_ids` list
2. Evict the extension's spec from the Governance's `REGISTERED_EXTENSIONS` stable cache
3. Revoke the extension's treasury allowances

The extension remains permanently registered and continues to be callable via `ExecuteExtensionOperation` proposals, and its treasury allowances remain active. The DAO's only recourse would be to upgrade the extension to a patched WASM (if one exists), but if no patch is available, the vulnerable extension cannot be removed.

### Likelihood Explanation

Extensions are currently disabled on mainnet (`is_sns_extensions_enabled()` returns false for production, and `ALLOWED_EXTENSIONS` is intentionally empty after KongSwap ceased operations on April 6, 2026): [7](#0-6) 

However, the code gap is structural and will be present when extensions are re-enabled. Any SNS that registers an extension after re-enablement will be permanently unable to deregister it via governance. The likelihood is **medium** for future deployments and **low** for the current mainnet state.

### Recommendation

Add a `DeregisterExtension` proposal action (analogous to `DeregisterDappCanisters`) that:
1. Calls a new `deregister_extension` method on the SNS Root canister to remove the extension from `extension_canister_ids`
2. Calls `clear_registered_extension_cache` in SNS Governance to evict the extension's spec
3. Optionally revokes treasury allowances granted to the extension

The SNS Root's `clean_up_failed_register_extension` already demonstrates the correct removal logic (retaining only non-matching IDs): [8](#0-7) 

This logic should be exposed as a governance-accessible deregistration path.

### Proof of Concept

1. An SNS DAO passes a `RegisterExtension` proposal for a TreasuryManager extension. `perform_register_extension` executes successfully, adding the extension to Root's `extension_canister_ids` and Governance's `REGISTERED_EXTENSIONS` cache.
2. A critical vulnerability is discovered in the extension's WASM that allows unauthorized treasury withdrawals.
3. The SNS DAO attempts to pass a proposal to remove the extension. No such `Action` variant exists in the governance proto or in `perform_action`'s dispatch table.
4. The DAO can only attempt `UpgradeExtension` (if a patched WASM is available and approved), but cannot remove the extension outright.
5. The vulnerable extension remains registered, its treasury allowances remain active, and `ExecuteExtensionOperation` proposals targeting it remain valid. [9](#0-8)

### Citations

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L729-743)
```text
    // Register an SNS extension canister.
    //
    // Id = 17.
    RegisterExtension register_extension = 21;

    // Execute an SNS extension's operation.
    //
    // Id = 18.
    ExecuteExtensionOperation execute_extension_operation = 22;

    // Upgrade an SNS extension canister.
    //
    // Id = 19.
    UpgradeExtension upgrade_extension = 23;
  }
```

**File:** rs/sns/governance/src/governance.rs (L2180-2199)
```rust
            Action::AddGenericNervousSystemFunction(nervous_system_function) => {
                self.perform_add_generic_nervous_system_function(nervous_system_function)
            }
            Action::RemoveGenericNervousSystemFunction(id) => {
                self.perform_remove_generic_nervous_system_function(id)
            }
            Action::RegisterDappCanisters(register_dapp_canisters) => {
                self.perform_register_dapp_canisters(register_dapp_canisters)
                    .await
            }
            Action::RegisterExtension(register_extension) => {
                self.perform_register_extension(register_extension).await
            }
            Action::UpgradeExtension(upgrade_extension) => {
                self.perform_upgrade_extension(upgrade_extension).await
            }
            Action::DeregisterDappCanisters(deregister_dapp_canisters) => {
                self.perform_deregister_dapp_canisters(deregister_dapp_canisters)
                    .await
            }
```

**File:** rs/sns/root/src/lib.rs (L593-604)
```rust
        self_ref.with_borrow_mut(|state| {
            if let Some(extensions) = state.extensions.as_mut() {
                extensions.extension_canister_ids.push(canister_id);
            } else {
                let extension_canister_ids = vec![canister_id];
                state.extensions.replace(Extensions {
                    extension_canister_ids,
                });
            }
        });

        Ok(())
```

**File:** rs/sns/root/src/lib.rs (L628-638)
```rust
            self_ref.with_borrow_mut(|state| {
                let Some(extensions) = state.extensions.as_mut() else {
                    return;
                };

                extensions
                    .extension_canister_ids
                    .retain(|prior_extension_canister_id| {
                        prior_extension_canister_id != &extension_canister_id
                    });
            });
```

**File:** rs/sns/governance/src/extensions.rs (L48-54)
```rust
thread_local! {
    static ALLOWED_EXTENSIONS: RefCell<BTreeMap<[u8; 32], ExtensionSpec>> = const { RefCell::new(btreemap! {
        // This collection is intentionally left empty. The Kong Swap extension used to be here,
        // but they ceased operations on April 6, 2026. Consequently, that was removed
        // from this list.
    }) };
}
```

**File:** rs/sns/governance/src/extensions.rs (L544-555)
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
```

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
