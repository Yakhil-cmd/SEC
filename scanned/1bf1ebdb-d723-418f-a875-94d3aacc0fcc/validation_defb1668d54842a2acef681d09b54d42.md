### Title
Unsynchronized `ledger_suite_orchestrator_id` / `minter_id` Update Causes Permanent DoS of `add_ckerc20_token` - (File: rs/ethereum/cketh/minter/src/main.rs)

---

### Summary

The ckETH minter stores a `ledger_suite_orchestrator_id` that can be updated at any time via an NNS upgrade proposal. The Ledger Suite Orchestrator (LSO) stores a `minter_id` that is set only at `init` and has **no corresponding field in `UpgradeArg`**, making it impossible to update without a full redeployment. When the NNS updates the minter's `ledger_suite_orchestrator_id` to a new LSO, the old LSO's stored `minter_id` still points to the old minter, and the minter's `add_ckerc20_token` guard rejects all calls from the old LSO. This permanently DoS-es the ability to register new ckERC20 tokens via the old LSO, with no atomic recovery path.

---

### Finding Description

The ckETH minter's `add_ckerc20_token` endpoint enforces a strict caller check against the locally stored `ledger_suite_orchestrator_id`:

```rust
// rs/ethereum/cketh/minter/src/main.rs:563-574
async fn add_ckerc20_token(erc20_token: AddCkErc20Token) {
    let orchestrator_id = read_state(|s| s.ledger_suite_orchestrator_id)
        .unwrap_or_else(|| ic_cdk::trap("ERROR: ERC-20 feature is not activated"));
    if orchestrator_id != ic_cdk::api::msg_caller() {
        ic_cdk::trap(format!(
            "ERROR: only the orchestrator {orchestrator_id} can add ERC-20 tokens"
        ));
    }
    ...
}
``` [1](#0-0) 

The minter's `ledger_suite_orchestrator_id` is an updatable field in `UpgradeArg`:

```
// rs/ethereum/cketh/minter/cketh_minter.did:129
ledger_suite_orchestrator_id : opt principal;
``` [2](#0-1) 

The LSO stores `minter_id` in its `State` struct, set at `init` via `InitArg`:

```rust
// rs/ethereum/ledger-suite-orchestrator/src/state/mod.rs:525
minter_id: Option<Principal>,
``` [3](#0-2) 

However, the LSO's `UpgradeArg` has **no `minter_id` field**:

```rust
// rs/ethereum/ledger-suite-orchestrator/src/candid/mod.rs:22-29
pub struct UpgradeArg {
    pub git_commit_hash: Option<String>,
    pub ledger_compressed_wasm_hash: Option<String>,
    pub index_compressed_wasm_hash: Option<String>,
    pub archive_compressed_wasm_hash: Option<String>,
    pub cycles_management: Option<UpdateCyclesManagement>,
    pub manage_ledger_suites: Option<Vec<InstalledLedgerSuite>>,
}
``` [4](#0-3) 

The `post_upgrade` handler for the LSO processes only the fields present in `UpgradeArg` and never touches `minter_id`: [5](#0-4) 

When the LSO fires its timer task to notify the minter of a new ERC20 token, it calls `add_ckerc20_token` on the stored `minter_id`:

```rust
// rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs:1141-1144
runtime
    .call_canister(*minter_id, "add_ckerc20_token", args)
    .await
    .map_err(TaskError::InterCanisterCallError)
``` [6](#0-5) 

The two canister IDs are therefore stored independently with no synchronization mechanism:

| Canister | Stored field | Updatable via upgrade? |
|---|---|---|
| ckETH minter | `ledger_suite_orchestrator_id` | **Yes** (`UpgradeArg`) |
| LSO | `minter_id` | **No** (init-only) |

---

### Impact Explanation

If the NNS submits an upgrade proposal for the ckETH minter that sets `ledger_suite_orchestrator_id` to a new LSO canister ID (e.g., during a major LSO migration), the old LSO's `minter_id` still points to the old minter. Every subsequent call from the old LSO to `add_ckerc20_token` on the old minter will trap with `"ERROR: only the orchestrator {new_lso_id} can add ERC-20 tokens"`. The old LSO's pending `NotifyErc20Added` timer tasks will retry indefinitely and always fail.

The impact is:
- **Permanent DoS of new ckERC20 token registration** via the old LSO.
- All in-flight `AddErc20Arg` proposals that were approved but not yet fully executed (ledger/index created, minter not yet notified) are permanently stuck.
- Recovery requires either reverting the minter's `ledger_suite_orchestrator_id` (another NNS proposal) or redeploying the LSO entirely and migrating all managed canisters — a complex, multi-step operation with no atomic path.

The exacerbating factor (analogous to the original report) is that if a new LSO2 is deployed with `minter_id = M1` and the minter is updated to `ledger_suite_orchestrator_id = LSO2`, then reverting the minter back to `ledger_suite_orchestrator_id = LSO1` breaks LSO2. There is no state where both LSOs can simultaneously add tokens to the same minter.

---

### Likelihood Explanation

The NNS controls both the minter and the LSO and can submit upgrade proposals for either independently. A realistic trigger is an NNS-approved migration to a new LSO version that requires a fresh canister deployment (e.g., incompatible stable memory layout). The NNS would:
1. Deploy LSO2 with `minter_id = M1`.
2. Upgrade M1 with `ledger_suite_orchestrator_id = LSO2`.

At this point LSO1 is broken. Since `minter_id` is not in `UpgradeArg`, there is no NNS proposal type that can fix LSO1 without a full redeployment. The likelihood is **medium**: it requires a deliberate NNS governance action, but the action is a routine upgrade operation with no in-protocol guard preventing the desynchronization.

---

### Recommendation

Add `minter_id: Option<Principal>` to the LSO's `UpgradeArg` and handle it in `post_upgrade`, mirroring how the minter handles `ledger_suite_orchestrator_id` in its own `UpgradeArg`. This allows the NNS to atomically update both sides in a single coordinated pair of upgrade proposals, or at minimum to recover from a desynchronized state without a full LSO redeployment.

```rust
// rs/ethereum/ledger-suite-orchestrator/src/candid/mod.rs
pub struct UpgradeArg {
    pub git_commit_hash: Option<String>,
    pub ledger_compressed_wasm_hash: Option<String>,
    pub index_compressed_wasm_hash: Option<String>,
    pub archive_compressed_wasm_hash: Option<String>,
    pub cycles_management: Option<UpdateCyclesManagement>,
    pub manage_ledger_suites: Option<Vec<InstalledLedgerSuite>>,
    pub minter_id: Option<Principal>,  // <-- add this
}
```

And in `post_upgrade`:
```rust
if let Some(new_minter_id) = arg.minter_id {
    mutate_state(|s| s.set_minter_id(new_minter_id));
}
```

---

### Proof of Concept

**Setup:**
1. LSO1 is deployed: `InitArg { minter_id: Some(M1), ... }` → `LSO1.minter_id = M1`.
2. Minter M1 is upgraded: `UpgradeArg { ledger_suite_orchestrator_id: Some(LSO1), ... }` → `M1.ledger_suite_orchestrator_id = LSO1`.
3. Several ERC20 tokens are added via LSO1 → M1 successfully.

**Trigger:**
4. NNS deploys LSO2: `InitArg { minter_id: Some(M1), ... }`.
5. NNS upgrades M1: `UpgradeArg { ledger_suite_orchestrator_id: Some(LSO2), ... }`.

**Result:**
6. LSO1 fires its timer for a pending `NotifyErc20Added` task, calling `M1.add_ckerc20_token(...)` with `caller = LSO1`.
7. M1 executes: `orchestrator_id = LSO2`, `msg_caller() = LSO1` → `LSO2 != LSO1` → `ic_cdk::trap("ERROR: only the orchestrator LSO2 can add ERC-20 tokens")`.
8. `TaskError::InterCanisterCallError` is returned; LSO1 schedules a retry. The retry always fails.
9. LSO1 cannot add any new ckERC20 tokens to M1. LSO1's `minter_id` cannot be updated without a full redeployment. [7](#0-6) [8](#0-7) [4](#0-3) [9](#0-8)

### Citations

**File:** rs/ethereum/cketh/minter/src/main.rs (L562-574)
```rust
#[update]
async fn add_ckerc20_token(erc20_token: AddCkErc20Token) {
    let orchestrator_id = read_state(|s| s.ledger_suite_orchestrator_id)
        .unwrap_or_else(|| ic_cdk::trap("ERROR: ERC-20 feature is not activated"));
    if orchestrator_id != ic_cdk::api::msg_caller() {
        ic_cdk::trap(format!(
            "ERROR: only the orchestrator {orchestrator_id} can add ERC-20 tokens"
        ));
    }
    let ckerc20_token = erc20::CkErc20Token::try_from(erc20_token)
        .unwrap_or_else(|e| ic_cdk::trap(format!("ERROR: {e}")));
    mutate_state(|s| process_event(s, EventType::AddedCkErc20Token(ckerc20_token)));
}
```

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L127-130)
```text
    // The principal of the ledger suite orchestrator that handles the ICRC1 ledger suites
    // for all ckERC20 tokens.
    ledger_suite_orchestrator_id : opt principal;

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

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L1122-1148)
```rust
async fn notify_erc20_added<R: CanisterRuntime>(
    token: &Erc20Token,
    minter_id: &Principal,
    runtime: &R,
) -> Result<(), TaskError> {
    let token_id = TokenId::from(token.clone());
    let managed_canisters = read_state(|s| s.managed_canisters(&token_id).cloned());
    match managed_canisters {
        Some(Canisters {
            ledger: Some(ledger),
            metadata,
            ..
        }) => {
            let args = AddCkErc20Token {
                chain_id: Nat::from(*token.chain_id().as_ref()),
                address: token.address().to_string(),
                ckerc20_token_symbol: metadata.token_symbol,
                ckerc20_ledger_id: *ledger.canister_id(),
            };
            runtime
                .call_canister(*minter_id, "add_ckerc20_token", args)
                .await
                .map_err(TaskError::InterCanisterCallError)
        }
        _ => Err(TaskError::LedgerNotFound(token.clone())),
    }
}
```
