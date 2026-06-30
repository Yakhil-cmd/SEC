### Title
Old XCC Router Contracts Are Permanently Non-Upgradable, Freezing Funds on Engine Interface Changes — (`engine/src/xcc.rs`)

---

### Summary

The Aurora Engine's XCC subsystem permanently excludes "pre-upgradable" router contracts from ever being upgraded. If the engine is later upgraded in a way that changes the `PromiseArgs` borsh format or the router method interface, those old routers will fail to process any XCC call, and the NEAR stored inside them (storage staking + any user-funded NEAR) will be permanently frozen with no recovery path.

---

### Finding Description

In `engine/src/xcc.rs`, the `AddressVersionStatus::new()` function classifies each user's deployed XCC router contract:

```rust
Some(version) if version < first_upgradable_version => {
    // It is impossible to upgrade the initial XCC routers because
    // they lack the upgrade method.
    Self::UpToDate
}
``` [1](#0-0) 

Any router deployed before `update_router_code` was first called (i.e., before the upgrade mechanism was introduced) has a `CodeVersion` below `first_upgradable_version`. The engine permanently classifies these as `UpToDate` and will never attempt to redeploy or upgrade them.

`first_upgradable_version` is set the first time `update_router_code` is called:

```rust
let key = storage::bytes_to_key(KeyPrefix::CrossContractCall, FIRST_UPGRADABLE);
if io.read_storage(&key).is_none() {
    let version_bytes = latest_version.0.to_le_bytes();
    io.write_storage(&key, &version_bytes);
}
``` [2](#0-1) 

Once set, any router with a version below this threshold is permanently locked out of the upgrade path. The engine will continue to call `execute` or `schedule` on these old routers:

```rust
pub fn execute(&self, #[serializer(borsh)] promise: PromiseArgs) {
    self.assert_preconditions();
    let promise_id = Self::promise_create(promise);
    env::promise_return(promise_id);
}
``` [3](#0-2) 

The `PromiseArgs` type is borsh-serialized by the engine and borsh-deserialized by the router. If the engine is upgraded to add a new `PromiseArgs` variant or change the serialization format, old routers will fail to deserialize the input and panic, causing every XCC call from those users to fail.

The NEAR stored in old routers (at minimum `STORAGE_AMOUNT = 2 NEAR` per router, plus any user-funded NEAR) has no guaranteed recovery path. The engine can only interact with old routers by calling their exposed functions. If the old router lacks `send_refund` or any equivalent fund-recovery function, the NEAR is permanently frozen. [4](#0-3) 

---

### Impact Explanation

**Impact: High (Temporary freezing of funds) → Critical (Permanent freezing of funds)**

- All XCC calls from users with old routers fail permanently after an engine upgrade that changes the `PromiseArgs` format or router method interface.
- The NEAR locked in old routers (2 NEAR storage staking + any additional user-funded NEAR) is permanently inaccessible if the old router contract lacks a fund-recovery function.
- There is no admin escape hatch: the engine cannot force-upgrade old routers (they lack `deploy_upgrade`), and NEAR accounts can only be drained via their own contract functions.

---

### Likelihood Explanation

**Likelihood: Medium**

- The engine has already changed the router interface once (introducing the upgrade mechanism itself), proving that interface changes do occur.
- The engine is a NEAR contract and is actively maintained and upgraded.
- Any future engine upgrade that adds a new `PromiseArgs` variant (e.g., `PromiseArgs::Recursive` was already added as a third variant) or changes the borsh encoding of existing types would trigger this.
- Users who deployed XCC routers before the upgrade mechanism was introduced have no way to protect themselves — they cannot trigger an upgrade of their own router.

---

### Recommendation

1. **Track old routers explicitly**: Instead of silently treating old routers as `UpToDate`, record their account IDs and provide an admin-callable function to force-delete and recreate them (transferring any NEAR balance to the user first).
2. **Provide a fund-recovery path**: Ensure all deployed router versions (including old ones) expose a function callable by the engine to drain NEAR back to the user's EVM address or to the engine.
3. **Freeze the `PromiseArgs` format**: Treat the borsh encoding of `PromiseArgs` as a stable ABI. Any new functionality should be added via new methods rather than new variants, to preserve backward compatibility with old routers.
4. **Emit an event or log** when a router is classified as non-upgradable, so operators can monitor the population of permanently-stuck routers.

---

### Proof of Concept

1. User Alice deploys an XCC router at version `V_old` (before `update_router_code` was first called, so `V_old < first_upgradable_version`).
2. Alice's router is deployed with 2 NEAR storage staking. Alice also funds it with additional NEAR via `fund_xcc_sub_account`.
3. The Aurora Engine team upgrades the engine and calls `update_router_code` with a new router binary that adds a new `PromiseArgs::NewVariant`.
4. Alice calls the XCC precompile from her EVM contract, triggering `handle_precompile_promise`.
5. `AddressVersionStatus::new()` returns `UpToDate` for Alice's router (because `V_old < first_upgradable_version`), so no upgrade is attempted.
6. The engine calls `execute` on Alice's old router with borsh-encoded `PromiseArgs::NewVariant`.
7. Alice's old router fails to deserialize the unknown variant and panics with a borsh error.
8. Alice's XCC call fails. All future XCC calls from Alice fail identically.
9. The NEAR in Alice's router is permanently inaccessible if the old router lacks `send_refund`. [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** engine/src/xcc.rs (L206-282)
```rust
    let latest_code_version = get_latest_code_version(io);
    let sender_code_version = get_code_version_of_address(io, &sender);
    let deploy_needed = AddressVersionStatus::new(io, latest_code_version, sender_code_version);
    // 1. If the router contract account does not exist or is out of date then we start
    //    with a batch transaction to deploy the router. This batch also has an attached
    //    callback to update the engine's storage with the new version of that router account.
    let setup_id = match &deploy_needed {
        AddressVersionStatus::DeployNeeded { create_needed } => {
            let mut promise_actions = Vec::with_capacity(4);
            let code = get_router_code(io).0.into_owned();
            // After the deployment we will call the contract's initialize function
            let wnear_address = get_wnear_address(io);
            let wnear_account = crate::engine::nep141_erc20_map(*io)
                .lookup_right(&crate::engine::ERC20Address(wnear_address))
                .expect("wnear account not found");
            let init_args = format!(
                r#"{{"wnear_account": "{}", "must_register": {}}}"#,
                wnear_account.0.as_ref(),
                create_needed,
            );
            if *create_needed {
                promise_actions.push(PromiseAction::CreateAccount);
                promise_actions.push(PromiseAction::Transfer {
                    amount: STORAGE_AMOUNT,
                });
                promise_actions.push(PromiseAction::DeployContract { code });
                promise_actions.push(PromiseAction::FunctionCall {
                    name: "initialize".into(),
                    args: init_args.into_bytes(),
                    attached_yocto: ZERO_YOCTO,
                    gas: INITIALIZE_GAS,
                });
            } else {
                let deploy_args = DeployUpgradeParams {
                    code,
                    initialize_args: init_args.into_bytes(),
                };
                promise_actions.push(PromiseAction::FunctionCall {
                    name: "deploy_upgrade".into(),
                    args: borsh::to_vec(&deploy_args).expect(ERR_UPGRADE_ARG_SERIALIZATION),
                    attached_yocto: ZERO_YOCTO,
                    gas: UPGRADE_GAS + INITIALIZE_GAS,
                });
            }

            let batch = PromiseBatchAction {
                target_account_id: promise.target_account_id.clone(),
                actions: promise_actions,
            };
            // Safety: This batch creation is safe because it only acts on the router sub-account
            // (not the main engine account), and the actions performed are only (1) create it
            // for the first time and/or (2) deploy the code from our storage (i.e. the deployed
            // code is controlled by us, not the user).
            let promise_id = match base_id {
                Some(id) => handler.promise_attach_batch_callback(id, &batch),
                None => handler.promise_create_batch(&batch),
            };
            // Add a callback here to update the version of the account
            let args = AddressVersionUpdateArgs {
                address: sender,
                version: latest_code_version,
            };
            let callback = PromiseCreateArgs {
                target_account_id: current_account_id.clone(),
                method: "factory_update_address_version".into(),
                args: borsh::to_vec(&args).unwrap(),
                attached_balance: ZERO_YOCTO,
                attached_gas: VERSION_UPDATE_GAS,
            };

            // Safety: A call from the engine to the engine's `factory_update_address_version`
            // method is safe because that method only writes the specific router sub-account
            // metadata that has just been deployed above.
            Some(handler.promise_attach_callback(promise_id, &callback))
        }
        AddressVersionStatus::UpToDate => base_id,
    };
```

**File:** engine/src/xcc.rs (L360-365)
```rust
    let key = storage::bytes_to_key(KeyPrefix::CrossContractCall, FIRST_UPGRADABLE);
    if io.read_storage(&key).is_none() {
        let version_bytes = latest_version.0.to_le_bytes();
        io.write_storage(&key, &version_bytes);
    }

```

**File:** engine/src/xcc.rs (L453-476)
```rust
impl AddressVersionStatus {
    fn new<I: IO>(
        io: &I,
        latest_code_version: CodeVersion,
        target_code_version: Option<CodeVersion>,
    ) -> Self {
        let first_upgradable_version =
            get_first_upgradable_version(io).unwrap_or(CodeVersion::ZERO);
        match target_code_version {
            None => Self::DeployNeeded {
                create_needed: true,
            },
            Some(version) if version < first_upgradable_version => {
                // It is impossible to upgrade the initial XCC routers because
                // they lack the upgrade method.
                Self::UpToDate
            }
            Some(version) if version < latest_code_version => Self::DeployNeeded {
                create_needed: false,
            },
            Some(_version) => Self::UpToDate,
        }
    }
}
```

**File:** etc/xcc-router/src/lib.rs (L50-62)
```rust
pub struct Router {
    /// The account id of the Aurora Engine instance that controls this router.
    parent: LazyOption<AccountId>,
    /// The version of the router contract that was last deployed
    version: LazyOption<u32>,
    /// A sequential id to keep track of how many scheduled promises this router has executed.
    /// This allows multiple promises to be scheduled before any of them are executed.
    nonce: LazyOption<u64>,
    /// The storage for the scheduled promises.
    scheduled_promises: LookupMap<u64, PromiseArgs>,
    /// Account ID for the wNEAR contract.
    wnear_account: AccountId,
}
```

**File:** etc/xcc-router/src/lib.rs (L128-133)
```rust
    pub fn execute(&self, #[serializer(borsh)] promise: PromiseArgs) {
        self.assert_preconditions();

        let promise_id = Self::promise_create(promise);
        env::promise_return(promise_id);
    }
```

**File:** etc/xcc-router/src/lib.rs (L176-184)
```rust
    pub fn send_refund(&self) -> Promise {
        let parent = self.get_parent().unwrap_or_else(env_panic);

        require_caller(&parent)
            .and_then(|_| require_no_failed_promises())
            .unwrap_or_else(env_panic);

        Promise::new(parent).transfer(REFUND_AMOUNT)
    }
```
