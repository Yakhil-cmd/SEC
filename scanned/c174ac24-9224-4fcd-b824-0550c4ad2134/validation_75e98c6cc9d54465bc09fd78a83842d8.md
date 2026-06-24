### Title
Ledger Suite Orchestrator `minter_id` Cannot Be Updated After Initialization, Creating Permanent State Divergence with ckETH Minter — (File: `rs/ethereum/ledger-suite-orchestrator/src/lifecycle/mod.rs`)

---

### Summary

The Ledger Suite Orchestrator (LSO) stores the ckETH minter's principal (`minter_id`) at initialization time and provides no mechanism to update it via `UpgradeArg`. Meanwhile, the ckETH minter stores the orchestrator's principal (`ledger_suite_orchestrator_id`) and **can** update it via upgrade. If the minter is ever replaced with a new canister principal, the orchestrator's `minter_id` becomes permanently stale, silently breaking the `NotifyErc20Added` flow for all subsequently added ckERC-20 tokens.

---

### Finding Description

The `State` struct in the Ledger Suite Orchestrator holds `minter_id: Option<Principal>`, which is populated once from `InitArg` during `init()`:

```rust
// rs/ethereum/ledger-suite-orchestrator/src/state/mod.rs
pub struct State {
    ...
    minter_id: Option<Principal>,
    ...
}
``` [1](#0-0) 

This value is set from `InitArg.minter_id` at construction: [2](#0-1) 

The `UpgradeArg` struct, however, contains **no `minter_id` field**:

```rust
pub struct UpgradeArg {
    pub git_commit_hash: Option<String>,
    pub ledger_compressed_wasm_hash: Option<String>,
    pub index_compressed_wasm_hash: Option<String>,
    pub archive_compressed_wasm_hash: Option<String>,
    pub cycles_management: Option<UpdateCyclesManagement>,
    pub manage_ledger_suites: Option<Vec<InstalledLedgerSuite>>,
}
``` [3](#0-2) 

The `post_upgrade` handler processes `UpgradeArg` but never touches `minter_id`: [4](#0-3) 

In contrast, the ckETH minter's `UpgradeArg` **does** expose `ledger_suite_orchestrator_id` as an updatable field:

```
type UpgradeArg = record {
    ...
    ledger_suite_orchestrator_id : opt principal;
    ...
};
``` [5](#0-4) 

The orchestrator uses its stored `minter_id` in the `NotifyErc20Added` task, which is scheduled every time a new ERC-20 token is installed:

```rust
read_state(|s| {
    let erc20_token = args.erc20_contract().clone();
    if let Some(&minter_id) = s.minter_id() {
        schedule_now(
            Task::NotifyErc20Added { erc20_token, minter_id },
            runtime,
        );
    }
});
``` [6](#0-5) 

The minter enforces that only the orchestrator (identified by `ledger_suite_orchestrator_id`) can call `add_ckerc20_token`: [7](#0-6) 

---

### Impact Explanation

If the ckETH minter is ever migrated to a new canister principal (e.g., via an NNS governance proposal that reinstalls the minter at a new ID), the orchestrator's `minter_id` becomes permanently stale. Every subsequent `AddErc20Arg` proposal executed through the orchestrator will schedule a `NotifyErc20Added` call to the **old, defunct minter principal**. The new minter will never receive these notifications and will never register the new ckERC-20 ledger IDs. As a result:

- All ckERC-20 tokens added after the minter migration are silently broken: the minter does not know their ledger IDs and cannot process deposits or withdrawals for them.
- The orchestrator cannot be corrected without a full reinstall, which would wipe all `managed_canisters` state (losing track of all existing ckERC-20 ledger/index canister IDs).
- There is no on-chain mechanism to detect or alert on this divergence.

This is a **chain-fusion mint/burn functionality break**: users attempting to deposit or withdraw newly added ckERC-20 tokens after a minter migration would find the minter unaware of those tokens.

---

### Likelihood Explanation

The NNS has full authority to upgrade or replace any NNS-controlled canister, including the ckETH minter (`sv3dd-oaaaa-aaaar-qacoa-cai`). A minter replacement (new canister ID) is a realistic operational scenario — for example, if a critical bug requires a fresh install rather than an in-place upgrade, or if the minter is migrated to a different subnet. The asymmetry is non-obvious: operators updating the minter's `ledger_suite_orchestrator_id` via `UpgradeArg` would naturally assume the reverse link is also updatable, but it is not. The probability is non-zero and the consequence is silent, permanent breakage of new ckERC-20 token support.

---

### Recommendation

Add a `minter_id: Option<Principal>` field to `UpgradeArg` in the Ledger Suite Orchestrator and handle it in `post_upgrade`:

```rust
// In UpgradeArg:
pub minter_id: Option<Principal>,

// In post_upgrade:
if let Some(new_minter_id) = arg.minter_id {
    mutate_state(|s| s.minter_id = Some(new_minter_id));
}
```

This mirrors the existing pattern for `ledger_suite_orchestrator_id` in the ckETH minter's `UpgradeArg`, ensuring both sides of the relationship can be kept in sync after any operational change.

---

### Proof of Concept

1. Deploy the orchestrator with `minter_id = Some(minter_v1_principal)`.
2. Via NNS governance, reinstall the ckETH minter at a new canister ID (`minter_v2_principal`). Configure the new minter with `ledger_suite_orchestrator_id = orchestrator_principal`.
3. Submit an NNS proposal to add a new ERC-20 token via the orchestrator (`AddErc20Arg`).
4. The orchestrator installs the new ledger/index canisters and schedules `NotifyErc20Added { minter_id: minter_v1_principal }`.
5. The call goes to the defunct `minter_v1_principal` (or fails with canister-not-found).
6. `minter_v2` never receives the notification; `get_minter_info` on `minter_v2` shows no entry for the new token.
7. Any user attempting to deposit the new ERC-20 token receives no ckERC-20 mint; withdrawal is also impossible.
8. The orchestrator has no `UpgradeArg.minter_id` field to correct this — the only recovery is a full orchestrator reinstall, losing all `managed_canisters` state.

### Citations

**File:** rs/ethereum/ledger-suite-orchestrator/src/state/mod.rs (L518-530)
```rust
#[derive(Clone, PartialEq, Debug, Deserialize, Serialize)]
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

**File:** rs/ethereum/ledger-suite-orchestrator/src/state/mod.rs (L768-788)
```rust
impl TryFrom<InitArg> for State {
    type Error = InvalidStateError;
    fn try_from(
        InitArg {
            more_controller_ids,
            minter_id,
            cycles_management,
        }: InitArg,
    ) -> Result<Self, Self::Error> {
        let state = Self {
            managed_canisters: Default::default(),
            completed_upgrades: Default::default(),
            cycles_management: cycles_management.unwrap_or_default(),
            more_controller_ids,
            minter_id,
            ledger_suite_version: Default::default(),
            active_tasks: Default::default(),
        };
        state.validate_config()?;
        Ok(state)
    }
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/candid/mod.rs (L21-29)
```rust
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

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L114-146)
```text
type UpgradeArg = record {
    // Change the nonce of the next transaction to be sent to the Ethereum network.
    next_transaction_nonce : opt nat;

    // Change the minimum amount in Wei that can be withdrawn.
    minimum_withdrawal_amount : opt nat;

    // Change the ETH helper smart contract address.
    ethereum_contract_address : opt text;

    // Change the ethereum block height observed by the minter.
    ethereum_block_height : opt BlockTag;

    // The principal of the ledger suite orchestrator that handles the ICRC1 ledger suites
    // for all ckERC20 tokens.
    ledger_suite_orchestrator_id : opt principal;

    // Change the ERC-20 helper smart contract address.
    erc20_helper_contract_address : opt text;

    // Change the last scraped block number of the ERC-20 helper smart contract.
    last_erc20_scraped_block_number : opt nat;

    // The principal of the EVM RPC canister that handles the communication
    // with the Ethereum blockchain.
    evm_rpc_id : opt principal;

    // Change the deposit with subaccount helper smart contract address.
    deposit_with_subaccount_helper_contract_address : opt text;

    // Change the last scraped block number of the deposit with subaccount helper smart contract.
    last_deposit_with_subaccount_scraped_block_number : opt nat;
};
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L878-889)
```rust
    read_state(|s| {
        let erc20_token = args.erc20_contract().clone();
        if let Some(&minter_id) = s.minter_id() {
            schedule_now(
                Task::NotifyErc20Added {
                    erc20_token,
                    minter_id,
                },
                runtime,
            );
        }
    });
```

**File:** rs/ethereum/cketh/minter/tests/ckerc20.rs (L44-64)
```rust
#[test]
fn should_refuse_to_add_ckerc20_token_from_unauthorized_principal() {
    let cketh = CkEthSetup::default();
    let result = cketh.add_ckerc20_token(Principal::anonymous(), &ckusdc());
    assert_matches!(result, Err(e) if e.code() == ErrorCode::CanisterCalledTrap && e.description().contains("ERROR: ERC-20"));

    let orchestrator_id: Principal = "nbsys-saaaa-aaaar-qaaga-cai".parse().unwrap();
    let result = cketh
        .upgrade_minter_to_add_orchestrator_id(orchestrator_id)
        .add_ckerc20_token(Principal::anonymous(), &ckusdc());
    assert_matches!(result, Err(e) if e.code() == ErrorCode::CanisterCalledTrap && e.description().contains("ERROR: only the orchestrator"));

    fn ckusdc() -> AddCkErc20Token {
        AddCkErc20Token {
            chain_id: Nat::from(1_u8),
            address: "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48".to_string(),
            ckerc20_token_symbol: "ckUSDC".to_string(),
            ckerc20_ledger_id: "mxzaz-hqaaa-aaaar-qaada-cai".parse().unwrap(),
        }
    }
}
```
