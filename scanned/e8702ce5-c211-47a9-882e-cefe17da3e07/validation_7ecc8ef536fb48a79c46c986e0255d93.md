### Title
Permanent Intermediate State in ckERC20 Token Addition Causes Silent Loss of ERC-20 Deposits - (File: `rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs`)

### Summary
Adding a new ckERC20 token to the IC chain-fusion stack requires five sequential async operations across multiple timer executions. If the final step — notifying the ckETH minter — fails with an unrecoverable error, the task is permanently discarded with no mechanism to re-trigger it. This leaves the system in a permanent intermediate state: the ledger and index canisters exist and are installed, but the minter never learns about the new token and never scrapes Ethereum logs for it. Any user who deposits the ERC-20 token to the Ethereum helper contract — whether during the transient window or after a permanent notification failure — will have their funds permanently locked on Ethereum with no ckERC20 minted.

### Finding Description

Adding a new ckERC20 token involves a multi-step async process executed across multiple timer invocations in `install_ledger_suite`:

1. Create ledger canister (`create_canister_once::<Ledger>`)
2. Create index canister (`create_canister_once::<Index>`)
3. Install ledger wasm (`install_canister_once::<Ledger>`)
4. Install index wasm (`install_canister_once::<Index>`)
5. Schedule `Task::NotifyErc20Added` → calls `add_ckerc20_token` on the minter [1](#0-0) 

The `NotifyErc20Added` task is the only mechanism by which the minter learns about the new token. Until this succeeds, the minter's `ckerc20_tokens` map does not contain the new token, and `ReceivedErc20LogScraping::next_scrape` will not include the new ERC-20 contract address in its log filter topics: [2](#0-1) 

The `run_task` function classifies errors as recoverable or unrecoverable. If unrecoverable, the task is permanently discarded: [3](#0-2) 

The `is_recoverable` function marks `Reason::Rejected` and `Reason::CanisterError` (unless the message ends with `"is stopped"` or `"is stopping"`) as unrecoverable: [4](#0-3) 

The minter's `add_ckerc20_token` endpoint traps (producing an unrecoverable `CanisterError`) in several cases — including if the ERC-20 feature is not activated, if the caller check fails, or if `record_add_ckerc20_token` panics (e.g., due to a duplicate symbol or ledger ID): [5](#0-4) [6](#0-5) 

When `NotifyErc20Added` is permanently discarded, there is no recovery path. A new `AddErc20Arg` NNS proposal for the same token would be rejected by the LSO with `Erc20ContractAlreadyManaged` because the ledger and index canisters already exist: [7](#0-6) 

This is confirmed by the existing test `should_not_reschedule_failed_task_with_irrecoverable_error`, which asserts the task queue is empty after an unrecoverable failure: [8](#0-7) 

**Transient intermediate state**: Even in the normal (non-failure) case, there is a window between NNS proposal execution and successful minter notification during which the minter does not scrape for the new token. Since the minter scrapes from `last_erc20_scraped_block_number` forward and does not backfill, any `ReceivedEthOrErc20` events emitted during this window are permanently missed. [9](#0-8) 

### Impact Explanation

**Permanent case**: If `NotifyErc20Added` fails with an unrecoverable error, the minter is permanently unaware of the new ckERC20 token. The minter will never scrape Ethereum logs for this token's contract address. Any user who deposits ERC-20 tokens to the Ethereum helper contract will have their funds permanently locked on Ethereum — the minter will never mint the corresponding ckERC20 tokens. The ledger and index canisters exist and are funded with cycles, but are permanently idle.

**Transient case**: During the normal multi-step addition process (spanning multiple timer executions), any ERC-20 deposit events emitted on Ethereum are permanently missed because the minter's `last_erc20_scraped_block_number` advances past them before the token is registered.

The documentation explicitly warns that the helper contract does not enforce a token whitelist and that unsupported token deposits are silently ignored: [10](#0-9) 

However, users have no way to distinguish between "token not yet supported" and "token addition in progress" — both look identical from the Ethereum side.

### Likelihood Explanation

**Transient**: Certain — every ckERC20 token addition passes through this intermediate state. The window spans multiple timer cycles (typically seconds to minutes). The risk is low per-addition but cumulative across all token additions.

**Permanent**: Low but non-zero. Realistic triggers include:
- A minter upgrade that introduces a bug in `add_ckerc20_token` (e.g., a validation regression that causes a trap)
- A race condition where the minter's `ledger_suite_orchestrator_id` is unset at the time of notification (e.g., minter upgrade in progress)
- A duplicate token symbol or ledger ID collision causing `record_add_ckerc20_token` to panic

The IC mainnet has already seen a related issue where a minter upgrade broke client behavior (`minter_upgrade_2024_11_30.md`), demonstrating that minter upgrades can introduce unexpected regressions. [11](#0-10) 

### Recommendation

1. **Make `NotifyErc20Added` always recoverable**: Change `is_recoverable` for `InterCanisterCallError` in the context of `NotifyErc20Added` to always return `true`, so the task retries indefinitely until it succeeds or is explicitly cancelled by governance.

2. **Add a governance-callable re-notification endpoint**: Add an NNS-callable endpoint on the LSO that re-schedules `NotifyErc20Added` for a specific token, allowing recovery from permanent failures without requiring a full re-addition.

3. **Track the Ethereum block number at proposal execution time**: When the minter is first notified of a new token, use the Ethereum block number at which the NNS proposal was executed as the starting scrape point for that token, rather than the current `last_erc20_scraped_block_number`. This prevents missed deposits during the intermediate state window.

4. **Atomic notification check**: Before marking the `InstallLedgerSuite` task as complete, verify that the minter notification succeeded within the same task execution, rather than scheduling it as a separate task.

### Proof of Concept

1. NNS executes proposal to add ckERC20 token X (e.g., a new stablecoin).
2. LSO timer fires: creates ledger canister, creates index canister, installs both.
3. LSO schedules `Task::NotifyErc20Added { erc20_token: X, minter_id }`.
4. Concurrently, a minter upgrade is executed that introduces a regression in `add_ckerc20_token` (e.g., a new validation check that traps for token X's parameters).
5. LSO timer fires: `notify_erc20_added` calls `add_ckerc20_token` on the minter.
6. Minter traps → `CanisterError("trap: ERROR: ...")` → `is_recoverable` returns `false`.
7. `run_task` discards the task permanently: `task_queue_from_state() == vec![]`.
8. User sees the NNS proposal executed and the token listed in `get_orchestrator_info` as managed.
9. User deposits token X to the Ethereum helper contract, emitting `ReceivedEthOrErc20` event.
10. Minter's `ReceivedErc20LogScraping::next_scrape` does not include token X's address in topics (token not in `ckerc20_tokens`).
11. Minter advances `last_erc20_scraped_block_number` past the deposit block.
12. A new `AddErc20Arg` proposal for token X fails with `Erc20ContractAlreadyManaged`.
13. User's ERC-20 tokens are permanently locked on Ethereum with no ckERC20 minted. [12](#0-11) [13](#0-12) [14](#0-13)

### Citations

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L185-223)
```rust
async fn run_task<R: CanisterRuntime>(task: TaskExecution, runtime: R) {
    const RETRY_FREQUENCY: Duration = Duration::from_secs(5);
    const ONE_HOUR: Duration = Duration::from_secs(60 * 60);

    if task.task_type.is_periodic() {
        schedule_after(ONE_HOUR, task.task_type.clone(), &runtime);
    }
    let _guard = match crate::guard::TimerGuard::new(task.task_type.clone()) {
        Some(guard) => guard,
        None => return,
    };
    let rerun_task_guard = scopeguard::guard(task.task_type.clone(), |task_type| {
        schedule_after(RETRY_FREQUENCY, task_type, &runtime);
    });
    let start = runtime.time();
    let result = task.execute(&runtime).await;
    let end = runtime.time();
    observe_task_duration(&task.task_type, &result, start, end);

    match result {
        Ok(()) => {
            let _task_type = ScopeGuard::into_inner(rerun_task_guard);
            log!(INFO, "task {:?} accomplished", task.task_type);
        }
        Err(e) => {
            if e.is_recoverable() {
                log!(INFO, "task {:?} failed: {:?}. Will retry later.", task, e);
            } else {
                let _task_type = ScopeGuard::into_inner(rerun_task_guard);
                log!(
                    INFO,
                    "ERROR: task {:?} failed with unrecoverable error: {:?}. Task is discarded.",
                    task,
                    e
                );
            }
        }
    }
}
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L560-564)
```rust
        if let Some(_canisters) = state.managed_canisters(&token_id) {
            return Err(InvalidAddErc20ArgError::Erc20ContractAlreadyManaged(
                contract,
            ));
        }
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L688-696)
```rust
fn is_recoverable(e: &CallError) -> bool {
    match &e.reason {
        Reason::OutOfCycles => true,
        Reason::CanisterError(msg) => msg.ends_with("is stopped") || msg.ends_with("is stopping"),
        Reason::Rejected(_) => false,
        Reason::TransientInternalError(_) => true,
        Reason::InternalError(_) => false,
    }
}
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L822-890)
```rust
async fn install_ledger_suite<R: CanisterRuntime>(
    args: &InstallLedgerSuiteArgs,
    runtime: &R,
) -> Result<(), TaskError> {
    record_new_erc20_token_once(
        args.contract.clone(),
        CanistersMetadata {
            token_symbol: args.ledger_init_arg.token_symbol.clone(),
        },
    );
    let CyclesManagement {
        cycles_for_ledger_creation,
        cycles_for_index_creation,
        cycles_for_archive_creation,
        ..
    } = read_state(|s| s.cycles_management().clone());
    let ledger_canister_id =
        create_canister_once::<Ledger, _>(&args.contract, runtime, cycles_for_ledger_creation)
            .await?;
    let index_principal =
        create_canister_once::<Index, _>(&args.contract, runtime, cycles_for_index_creation)
            .await?;

    let more_controllers = read_state(|s| s.more_controller_ids().to_vec())
        .into_iter()
        .map(PrincipalId)
        .collect();
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

**File:** rs/ethereum/cketh/minter/src/eth_logs/scraping.rs (L66-92)
```rust
impl LogScraping for ReceivedErc20LogScraping {
    const ID: LogScrapingId = LogScrapingId::Erc20DepositWithoutSubaccount;
    type Parser = ReceivedErc20LogParser;

    fn next_scrape(state: &State) -> Option<Scrape> {
        if state.ckerc20_tokens.is_empty() {
            return None;
        }
        let contract_address = *Self::contract_address(state)?;
        let last_scraped_block_number = Self::last_scraped_block_number(state);

        let mut topics: Vec<_> = vec![Topic::Single(Hex32::from(RECEIVED_ERC20_EVENT_TOPIC))];
        // We add token contract addresses as additional topics to match.
        // It has a disjunction semantics, so it will match if event matches any one of these addresses.
        topics.push(
            erc20_smart_contracts_addresses_as_topics(state)
                .collect::<Vec<_>>()
                .into(),
        );

        Some(Scrape {
            contract_address,
            last_scraped_block_number,
            topics,
        })
    }
}
```

**File:** rs/ethereum/cketh/minter/src/eth_logs/scraping.rs (L94-122)
```rust
pub enum ReceivedEthOrErc20LogScraping {}

impl LogScraping for ReceivedEthOrErc20LogScraping {
    const ID: LogScrapingId = LogScrapingId::EthOrErc20DepositWithSubaccount;
    type Parser = ReceivedEthOrErc20LogParser;

    fn next_scrape(state: &State) -> Option<Scrape> {
        let contract_address = *Self::contract_address(state)?;
        let last_scraped_block_number = Self::last_scraped_block_number(state);

        let mut topics: Vec<_> = vec![Topic::Single(Hex32::from(
            RECEIVED_ETH_OR_ERC20_WITH_SUBACCOUNT_EVENT_TOPIC,
        ))];
        // We add token contract addresses as additional topics to match.
        // It has a disjunction semantics, so it will match if event matches any one of these addresses.
        topics.push(
            once(Hex32::from([0_u8; 32]))
                .chain(erc20_smart_contracts_addresses_as_topics(state))
                .collect::<Vec<_>>()
                .into(),
        );

        Some(Scrape {
            contract_address,
            last_scraped_block_number,
            topics,
        })
    }
}
```

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

**File:** rs/ethereum/cketh/minter/src/state.rs (L398-424)
```rust
    pub fn record_add_ckerc20_token(&mut self, ckerc20_token: CkErc20Token) {
        assert_eq!(
            self.ethereum_network, ckerc20_token.erc20_ethereum_network,
            "ERROR: Expected {}, but got {}",
            self.ethereum_network, ckerc20_token.erc20_ethereum_network
        );
        let ckerc20_with_same_symbol = self
            .supported_ck_erc20_tokens()
            .filter(|ckerc20| ckerc20.ckerc20_token_symbol == ckerc20_token.ckerc20_token_symbol)
            .collect::<Vec<_>>();
        assert_eq!(
            ckerc20_with_same_symbol,
            vec![],
            "ERROR: ckERC20 token symbol {} is already used by {:?}",
            ckerc20_token.ckerc20_token_symbol,
            ckerc20_with_same_symbol
        );
        assert_eq!(
            self.ckerc20_tokens.try_insert(
                ckerc20_token.ckerc20_ledger_id,
                ckerc20_token.erc20_contract_address,
                ckerc20_token.ckerc20_token_symbol,
            ),
            Ok(()),
            "ERROR: some ckERC20 tokens use the same ckERC20 ledger ID or ERC-20 address"
        );
    }
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/tests.rs (L1331-1351)
```rust
    #[tokio::test]
    async fn should_not_reschedule_failed_task_with_irrecoverable_error() {
        init_state();
        record_added_usdc();
        let mut runtime = MockCanisterRuntime::new();
        runtime.expect_time().return_const(0_u64);
        runtime.expect_global_timer_set().return_const(());
        expect_call_canister_add_ckerc20_token(
            &mut runtime,
            MINTER_PRINCIPAL,
            add_ckusdc(),
            Err(CallError {
                method: "error".to_string(),
                reason: Reason::CanisterError("trap".to_string()),
            }),
        );

        run_task(notify_usdc_added_task(), runtime).await;

        assert_eq!(task_queue_from_state(), vec![]);
    }
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L182-191)
```text
[WARNING]
.Supported ERC-20 tokens
====
Note that the helper smart contract does not enforce any whitelist of allowed ERC-20 tokens. This is enforced by the minter, which fetches logs only for the supported ERC-20 tokens. Therefore, funds of unsupported ERC-20 tokens could be deposited via the helper smart contract, but the minter will not know anything about it. To avoid any loss of funds, please verify **before** any important transfer that the desired ERC-20 token is supported by querying the minter as follows
and checking the field `supported_ckerc20_tokens`:
[source,shell]
----
dfx canister --network ic call minter get_minter_info
----
====
```

**File:** rs/ethereum/cketh/mainnet/minter_upgrade_2024_11_30.md (L17-23)
```markdown
## Motivation

Fix an undesired breaking changed introduced by proposal [134264](https://dashboard.internetcomputer.org/proposal/134264) :

1. The fields `eth_helper_contract_address` and `erc20_helper_contract_address` in `get_minter_info` were wrongly reused to point to the new helper smart contract [0x18901044688D3756C35Ed2b36D93e6a5B8e00E68](https://etherscan.io/address/0x18901044688D3756C35Ed2b36D93e6a5B8e00E68) that supports deposit with subaccounts and that was added as part of proposal [134264](https://dashboard.internetcomputer.org/proposal/134264).
2. This broke clients that relied on that information to make deposit of ETH or ERC-20 because the new helper smart contract has a different ABI. This is visible by such a [transaction](https://etherscan.io/tx/0x0968b25814221719bf966cf4bbd2de8290ed2ab42c049d451d64e46812d1574e), where the transaction tried to call the method `deposit` (`0xb214faa5`) that does exist on the [deprecated ETH helper smart contract](https://etherscan.io/address/0x7574eB42cA208A4f6960ECCAfDF186D627dCC175) but doesn't on the new contract (it should have been `depositEth` (`0x17c819c4`)).
3. The fix simply consists in reverting the changes regarding the values of the fields `eth_helper_contract_address` and `erc20_helper_contract_address` in `get_minter_info` (so that they point back to [0x7574eB42cA208A4f6960ECCAfDF186D627dCC175](https://etherscan.io/address/0x7574eB42cA208A4f6960ECCAfDF186D627dCC175) and [0x6abDA0438307733FC299e9C229FD3cc074bD8cC0](https://etherscan.io/address/0x6abDA0438307733FC299e9C229FD3cc074bD8cC0), respectively) and adding new fields to contain the state of the log scraping (address and last scraped block number) for the new helper smart contract.
```
