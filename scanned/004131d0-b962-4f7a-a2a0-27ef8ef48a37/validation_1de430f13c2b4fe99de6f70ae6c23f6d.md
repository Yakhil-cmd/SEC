### Title
`minter_id` Immutably Stored at Init with No Upgrade Path in Ledger Suite Orchestrator - (File: rs/ethereum/ledger-suite-orchestrator/src/candid/mod.rs)

---

### Summary

The `ledger-suite-orchestrator` canister stores `minter_id` at `init` time and provides no mechanism to update it via `UpgradeArg`. If the ckETH minter canister is ever redeployed at a new canister ID, the orchestrator's `NotifyErc20Added` task will permanently call the wrong (stale) principal, DOSing the entire ckERC-20 token addition flow without any on-chain recovery path short of a full reinstall that wipes all managed-canister state.

---

### Finding Description

`InitArg` accepts `minter_id: Option<Principal>` and stores it in `State`:

```rust
// rs/ethereum/ledger-suite-orchestrator/src/candid/mod.rs
pub struct InitArg {
    pub more_controller_ids: Vec<Principal>,
    pub minter_id: Option<Principal>,          // set once at init
    pub cycles_management: Option<CyclesManagement>,
}
```

`UpgradeArg`, the only struct accepted by `post_upgrade`, contains **no `minter_id` field**:

```rust
pub struct UpgradeArg {
    pub git_commit_hash: Option<String>,
    pub ledger_compressed_wasm_hash: Option<String>,
    pub index_compressed_wasm_hash: Option<String>,
    pub archive_compressed_wasm_hash: Option<String>,
    pub cycles_management: Option<UpdateCyclesManagement>,
    pub manage_ledger_suites: Option<Vec<InstalledLedgerSuite>>,
    // NO minter_id field
}
```

`post_upgrade` in `lifecycle/mod.rs` processes every field of `UpgradeArg` but never touches `minter_id` in `State`:

```rust
pub fn post_upgrade(upgrade_arg: Option<UpgradeArg>) {
    if let Some(arg) = upgrade_arg {
        // handles git_commit_hash, manage_ledger_suites, wasm upgrades, cycles_management
        // minter_id is never read or mutated
    }
    ...
}
```

The stored `minter_id` is the sole target for the `NotifyErc20Added` task, which is the critical cross-canister call that tells the minter about newly added ERC-20 tokens:

```rust
// rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs
pub enum Task {
    ...
    NotifyErc20Added {
        erc20_token: Erc20Token,
        minter_id: Principal,   // taken from state at add_erc20 time
    },
}
```

And in `validate_add_erc20`:

```rust
let minter_id =
    state
        .minter_id()
        .cloned()
        .ok_or(InvalidAddErc20ArgError::InternalError(
            "ERROR: minter principal not set in state".to_string(),
        ))?;
```

---

### Impact Explanation

If the ckETH minter (`sv3dd-oaaaa-aaaar-qacoa-cai`) is ever replaced by a new canister at a different principal (e.g., due to a critical security incident requiring a fresh deployment, or a subnet migration), the orchestrator will continue calling the old, stale `minter_id`. Every `NotifyErc20Added` task will fail with a rejection because the old canister no longer exists or no longer accepts those calls. New ckERC-20 token additions will be permanently DOSed. The only recovery is a full reinstall of the orchestrator, which destroys all `managed_canisters` state — the complete record of which ERC-20 tokens are managed and their associated ledger/index canister IDs.

---

### Likelihood Explanation

Canister IDs on the IC are permanent for a given canister, but canisters can be replaced by deploying a new canister at a new ID. The ckETH minter is an NNS-controlled canister; a critical bug or security incident could necessitate deploying a replacement minter at a new ID. The IC ecosystem has already demonstrated willingness to deploy new helper contract addresses (the `deposit_with_subaccount_helper_contract_address` was added via upgrade), showing that addresses/IDs do change. The probability is low but non-zero, and the impact when it occurs is a complete, unrecoverable DOS of the ckERC-20 addition flow.

---

### Recommendation

Add `minter_id: Option<Principal>` to `UpgradeArg` and handle it in `post_upgrade`:

```rust
pub struct UpgradeArg {
    ...
    pub minter_id: Option<Principal>,  // add this field
}
```

In `lifecycle/mod.rs`:

```rust
pub fn post_upgrade(upgrade_arg: Option<UpgradeArg>) {
    if let Some(arg) = upgrade_arg {
        if let Some(new_minter_id) = arg.minter_id {
            mutate_state(|s| s.set_minter_id(new_minter_id));
        }
        // ... existing handling
    }
}
```

This mirrors the pattern already used by the ckETH minter itself, whose `UpgradeArg` includes `evm_rpc_id: opt principal` to allow updating the EVM RPC canister ID post-deployment.

---

### Proof of Concept

1. Orchestrator is initialized with `minter_id = sv3dd-oaaaa-aaaar-qacoa-cai`.
2. The ckETH minter is redeployed at a new canister ID `new-minter-xxxx-cai` due to a critical bug.
3. An NNS proposal calls `add_erc20` on the orchestrator for a new token.
4. `validate_add_erc20` reads `state.minter_id()` → returns the stale `sv3dd-oaaaa-aaaar-qacoa-cai`.
5. `Task::NotifyErc20Added { minter_id: sv3dd-... }` is scheduled.
6. The task fires and calls `sv3dd-...` which no longer accepts `add_ckerc20_token` calls → rejection.
7. The task is retried indefinitely (recoverable error path), permanently blocking the ERC-20 addition flow.
8. No `UpgradeArg` field exists to correct `minter_id` without a full reinstall. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/ethereum/ledger-suite-orchestrator/src/candid/mod.rs (L14-29)
```rust
#[derive(Clone, Eq, PartialEq, Debug, Default, CandidType, Deserialize)]
pub struct InitArg {
    pub more_controller_ids: Vec<Principal>,
    pub minter_id: Option<Principal>,
    pub cycles_management: Option<CyclesManagement>,
}

#[derive(Clone, Eq, PartialEq, Debug, Default, CandidType, Deserialize)]
pub struct UpgradeArg {
    pub git_commit_hash: Option<String>,
    pub ledger_compressed_wasm_hash: Option<String>,
    pub index_compressed_wasm_hash: Option<String>,
    pub archive_compressed_wasm_hash: Option<String>,
    pub cycles_management: Option<UpdateCyclesManagement>,
    pub manage_ledger_suites: Option<Vec<InstalledLedgerSuite>>,
}
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/lifecycle/mod.rs (L25-86)
```rust
pub fn post_upgrade(upgrade_arg: Option<UpgradeArg>) {
    if let Some(arg) = upgrade_arg {
        if let Some(git_commit_hash) = &arg.git_commit_hash {
            let git_commit_hash =
                GitCommitHash::from_str(git_commit_hash).expect("ERROR: invalid git commit hash");
            let ledger_suite_version = mutate_wasm_store(|s| {
                record_icrc1_ledger_suite_wasms(s, ic_cdk::api::time(), git_commit_hash)
            })
            .expect("BUG: failed to record icrc1 ledger suite wasms during upgrade");
            mutate_state(|s| s.init_ledger_suite_version(ledger_suite_version));
        }
        if let Some(manage_installed_canisters) = arg.manage_ledger_suites.clone() {
            for managed_canisters in manage_installed_canisters {
                let canisters =
                    read_state(|s| InstalledLedgerSuite::validate(s, managed_canisters))
                        .expect("ERROR: invalid manage installed canisters");
                mutate_state(|s| s.record_manage_other_canisters(canisters.clone()));
                log!(
                    INFO,
                    "[post_upgrade]: recorded manage installed canisters: {:?}",
                    canisters
                );
            }
        }
        match read_wasm_store(|w| UpgradeOrchestratorArgs::validate_upgrade_arg(w, arg.clone())) {
            Ok(valid_upgrade_args) => {
                if valid_upgrade_args.upgrade_ledger_suite() {
                    let current_ledger_suite_version =
                        read_state(|s| s.ledger_suite_version().cloned())
                            .expect("BUG: missing ledger suite version");
                    mutate_state(|s| {
                        s.update_ledger_suite_version(
                            valid_upgrade_args
                                .clone()
                                .new_ledger_suite_version(current_ledger_suite_version),
                        )
                    });
                    for token_id in
                        read_state(|s| s.all_managed_tokens_ids_iter().collect::<Vec<_>>())
                    {
                        schedule_now(
                            Task::UpgradeLedgerSuite(
                                valid_upgrade_args.clone().into_task(token_id),
                            ),
                            &IC_CANISTER_RUNTIME,
                        );
                    }
                }
            }
            Err(e) => {
                ic_cdk::trap(format!(
                    "[post_upgrade]: ERROR: invalid arguments to upgrade {arg:?}: {e:?}"
                ));
            }
        }
        if let Some(update) = arg.cycles_management {
            mutate_state(|s| update.apply(s.cycles_management_mut()));
        }
    }
    read_state(|s| s.validate_config().expect("ERROR: invalid state"));
    setup_tasks_and_timers()
}
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L51-62)
```rust
#[allow(clippy::large_enum_variant)]
#[derive(Clone, Eq, PartialEq, Ord, PartialOrd, Debug, Deserialize, Serialize)]
pub enum Task {
    InstallLedgerSuite(InstallLedgerSuiteArgs),
    UpgradeLedgerSuite(UpgradeLedgerSuite),
    MaybeTopUp,
    DiscoverArchives,
    NotifyErc20Added {
        erc20_token: Erc20Token,
        minter_id: Principal,
    },
}
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L553-559)
```rust
        let minter_id =
            state
                .minter_id()
                .cloned()
                .ok_or(InvalidAddErc20ArgError::InternalError(
                    "ERROR: minter principal not set in state".to_string(),
                ))?;
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/state/mod.rs (L519-530)
```rust
pub struct State {
    managed_canisters: ManagedCanisters,
    #[serde(default)]
    completed_upgrades: BTreeMap<Principal, CanisterUpgrade>,
    cycles_management: CyclesManagement,
    more_controller_ids: Vec<Principal>,
    minter_id: Option<Principal>,
    /// Locks preventing concurrent execution timer tasks
    pub active_tasks: BTreeSet<Task>,
    #[serde(default)]
    ledger_suite_version: Option<LedgerSuiteVersion>,
}
```
