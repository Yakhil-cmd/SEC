### Title
Incomplete ckERC20 Token Initialization: Minter Notified with Ledger in `Created` (Uninstalled) State - (File: `rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs`)

### Summary

The `notify_erc20_added` function in the Ledger Suite Orchestrator (LSO) sends the new ckERC20 token's ledger canister ID to the ckETH minter via `add_ckerc20_token` without verifying that the ledger canister has actually had its Wasm module installed (i.e., is in `Installed` state rather than `Created` state). This creates a window where the minter begins scraping Ethereum logs and attempting to mint ckERC20 tokens against a ledger canister that has no Wasm installed and will reject all calls.

### Finding Description

The `install_ledger_suite` function in `rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs` performs ckERC20 token initialization across multiple sequential async steps:

1. Create ledger canister → state: `Created`
2. Create index canister → state: `Created`
3. Install Wasm on ledger → state: `Installed`
4. Install Wasm on index → state: `Installed`
5. Schedule `NotifyErc20Added` task

The `NotifyErc20Added` task is only scheduled after step 4 completes successfully. However, the `notify_erc20_added` function that executes this task reads the ledger canister ID from state and calls `add_ckerc20_token` on the minter **without checking whether the ledger's `ManagedCanisterStatus` is `Installed` or merely `Created`**:

```rust
// rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs:1129-1144
match managed_canisters {
    Some(Canisters {
        ledger: Some(ledger),
        metadata,
        ..
    }) => {
        let args = AddCkErc20Token {
            ...
            ckerc20_ledger_id: *ledger.canister_id(),  // no check on ledger.status()
        };
        runtime
            .call_canister(*minter_id, "add_ckerc20_token", args)
            .await
            ...
    }
```

The `ledger.canister_id()` method returns the principal regardless of whether the canister is `Created` or `Installed`:

```rust
// rs/ethereum/ledger-suite-orchestrator/src/state/mod.rs:444-449
pub fn canister_id(&self) -> &Principal {
    match self {
        ManagedCanisterStatus::Created { canister_id }
        | ManagedCanisterStatus::Installed { canister_id, .. } => canister_id,
    }
}
```

The test `should_notify_erc20_added` in `rs/ethereum/ledger-suite-orchestrator/src/scheduler/tests.rs` explicitly demonstrates that `NotifyErc20Added` succeeds when the ledger is only in `Created` state (no Wasm installed), confirming this is the actual runtime behavior.

Additionally, the `install_ledger_suite` function itself has a partial-initialization window: if the index Wasm installation fails (step 4), the ledger is already `Installed` but the index is still `Created`. On retry, `install_canister_once` for the ledger returns `Ok(())` immediately (idempotent), and the `NotifyErc20Added` task is scheduled. At this point the minter is notified of a ledger that has no corresponding functional index canister.

### Impact Explanation

Once the minter receives `add_ckerc20_token` with a ledger canister ID, it begins scraping Ethereum logs for that ERC-20 contract address and attempts to mint ckERC20 tokens by calling `icrc1_transfer` (mint) on the ledger. If the ledger canister has no Wasm installed (`Created` state), every mint call will be rejected by the IC management canister with a "canister has no wasm module" error. The minter will queue these failed mints and retry them. Depending on the minter's retry/error-handling logic, this can result in:

- Accepted ERC-20 deposits on Ethereum that are permanently stuck (user funds locked) if the minter marks the deposit as processed but the mint fails non-recoverably.
- Inconsistent minter state where `AcceptedErc20Deposit` events are recorded but no corresponding `MintedCkErc20` events follow.
- The minter's `supported_ckerc20_tokens` list advertising a token that cannot be minted, misleading users into making Ethereum deposits.

The impact is a **chain-fusion mint/burn integrity bug**: real ERC-20 tokens deposited on Ethereum during the initialization window cannot be minted as ckERC20 on the IC, potentially causing permanent loss of user funds if the minter's deduplication logic marks the deposit as seen before the ledger becomes operational.

### Likelihood Explanation

This window is narrow under normal conditions (the LSO retries on the next timer tick), but it is reachable by any unprivileged user who:
1. Observes the NNS proposal to add a new ckERC20 token being executed (public on-chain event).
2. Immediately submits an ERC-20 deposit to the helper contract on Ethereum.
3. The minter scrapes the log and attempts to mint before the LSO completes ledger installation.

The retry-based architecture means the window exists for at least one full timer interval (seconds to minutes). The scenario is more likely during transient failures (e.g., cycles exhaustion during index installation), which leave the system in the partial state indefinitely until the next successful retry.

### Recommendation

In `notify_erc20_added`, add an explicit check that the ledger's `ManagedCanisterStatus` is `Installed` before calling `add_ckerc20_token` on the minter:

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
            // Ensure ledger Wasm is installed before notifying minter
            match ledger.status() {
                ManagedCanisterStatus::Created { .. } => {
                    return Err(TaskError::LedgerNotReady(...));
                }
                ManagedCanisterStatus::Installed { canister_id, .. } => {
                    // proceed with notification
                }
            }
            ...
        }
    }
}
```

Similarly, verify the index canister is `Installed` before scheduling `NotifyErc20Added` in `install_ledger_suite`.

### Proof of Concept

The existing test at `rs/ethereum/ledger-suite-orchestrator/src/scheduler/tests.rs:464-494` (`should_notify_erc20_added`) directly demonstrates the bug: it sets up the ledger in `ManagedCanisterStatus::Created` state (no Wasm installed) and asserts that `NotifyErc20Added` succeeds and calls `add_ckerc20_token` on the minter. This confirms the minter is notified of a ledger principal that has no Wasm module installed.

```
mutate_state(|s| {
    s.record_new_erc20_token(usdc.clone(), usdc_metadata.clone());
    s.record_created_canister::<Ledger>(&usdc, LEDGER_PRINCIPAL); // Created, not Installed
});
// ... NotifyErc20Added task executes successfully and calls add_ckerc20_token
assert_eq!(task.execute(&runtime).await, Ok(()));
```

The `notify_erc20_added` function at line 1130 matches on `ledger: Some(ledger)` without inspecting `ledger.status()`, so a `Created`-state ledger passes the match and its principal is forwarded to the minter. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L849-891)
```rust
    install_canister_once::<Ledger, _, _>(
        &args.contract,
        &args.ledger_compressed_wasm_hash,
        &LedgerArgument::Init(icrc1_ledger_init_arg(
            args.minter_id,
            args.ledger_init_arg.clone(),
            runtime.id().into(),
            more_controllers,
            cycles_for_archive_creation,
            index_principal,
        )),
        runtime,
    )
    .await?;

    let index_arg = Some(IndexArg::Init(IndexInitArg {
        ledger_id: ledger_canister_id,
        #[allow(deprecated)]
        retrieve_blocks_from_ledger_interval_seconds: None,
        min_retrieve_blocks_from_ledger_interval_seconds: None,
        max_retrieve_blocks_from_ledger_interval_seconds: None,
    }));
    install_canister_once::<Index, _, _>(
        &args.contract,
        &args.index_compressed_wasm_hash,
        &index_arg,
        runtime,
    )
    .await?;
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
    Ok(())
}
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L1047-1057)
```rust
    let token_id = TokenId::from(contract.clone());
    let canister_id = match read_state(|s| s.managed_status::<C>(&token_id).cloned()) {
        None => {
            panic!(
                "BUG: {} canister is not yet created",
                Canisters::display_name()
            )
        }
        Some(ManagedCanisterStatus::Created { canister_id }) => canister_id,
        Some(ManagedCanisterStatus::Installed { .. }) => return Ok(()),
    };
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

**File:** rs/ethereum/ledger-suite-orchestrator/src/state/mod.rs (L444-460)
```rust
impl ManagedCanisterStatus {
    pub fn canister_id(&self) -> &Principal {
        match self {
            ManagedCanisterStatus::Created { canister_id }
            | ManagedCanisterStatus::Installed { canister_id, .. } => canister_id,
        }
    }

    fn installed_wasm_hash(&self) -> Option<&WasmHash> {
        match self {
            ManagedCanisterStatus::Created { .. } => None,
            ManagedCanisterStatus::Installed {
                installed_wasm_hash,
                ..
            } => Some(installed_wasm_hash),
        }
    }
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/tests.rs (L464-494)
```rust
    #[tokio::test]
    async fn should_notify_erc20_added() {
        init_state();
        let usdc = usdc();
        let usdc_metadata = usdc_metadata();
        mutate_state(|s| {
            s.record_new_erc20_token(usdc.clone(), usdc_metadata.clone());
            s.record_created_canister::<Ledger>(&usdc, LEDGER_PRINCIPAL);
        });
        let task = TaskExecution {
            task_type: Task::NotifyErc20Added {
                erc20_token: usdc.clone(),
                minter_id: MINTER_PRINCIPAL,
            },
            execute_at_ns: 0,
        };
        let mut runtime = MockCanisterRuntime::new();
        expect_call_canister_add_ckerc20_token(
            &mut runtime,
            MINTER_PRINCIPAL,
            AddCkErc20Token {
                chain_id: Nat::from(1_u8),
                address: usdc.address().to_string(),
                ckerc20_token_symbol: usdc_metadata.token_symbol,
                ckerc20_ledger_id: LEDGER_PRINCIPAL,
            },
            Ok(()),
        );

        assert_eq!(task.execute(&runtime).await, Ok(()));
    }
```
