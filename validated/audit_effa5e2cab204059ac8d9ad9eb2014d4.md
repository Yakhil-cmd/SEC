All cited code references are verified against the actual codebase. Here is the validation result:

---

Audit Report

## Title
Permanent Unrecoverable Failure of `NotifyErc20Added` Task Leaves ckERC20 Token in Broken State With No Recovery Path - (File: `rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs`)

## Summary
When the `Task::NotifyErc20Added` task fails with a `CanisterError` (e.g., a trap in the minter's `add_ckerc20_token` endpoint), `is_recoverable` returns `false` and `run_task` permanently discards the task. The LSO already has the token's canisters created and installed, so a new NNS proposal for the same token is rejected with `Erc20ContractAlreadyManaged`. The minter never learns about the token, never scrapes its Ethereum logs, and any ERC-20 deposits to the helper contract are permanently missed with no ckERC20 minted.

## Finding Description

`install_ledger_suite` completes all four canister setup steps and then schedules `Task::NotifyErc20Added` as a separate, independent task: [1](#0-0) 

`notify_erc20_added` calls `add_ckerc20_token` on the minter and wraps any failure as `TaskError::InterCanisterCallError`: [2](#0-1) 

`TaskError::is_recoverable` delegates to `is_recoverable(e)` for `InterCanisterCallError`: [3](#0-2) 

`is_recoverable` marks `Reason::CanisterError(msg)` as unrecoverable unless the message ends with `"is stopped"` or `"is stopping"`. A trap message such as `"trap: ERROR: ..."` does not match either suffix: [4](#0-3) 

`run_task` defuses the retry guard on an unrecoverable error, permanently dropping the task: [5](#0-4) 

The minter's `add_ckerc20_token` endpoint calls `ic_cdk::trap` in multiple cases — ERC-20 feature not activated, wrong caller, network mismatch, duplicate symbol, or duplicate ledger/address — all of which produce a `CanisterError` that `is_recoverable` classifies as unrecoverable: [6](#0-5) [7](#0-6) 

Once the task is discarded, there is no re-trigger mechanism. A new NNS proposal for the same token is rejected because `managed_canisters` already exists for the token ID: [8](#0-7) 

With the minter unaware of the token, `ReceivedErc20LogScraping::next_scrape` never includes the token's contract address in its log filter topics, and `last_scraped_block_number` advances past any deposit blocks: [9](#0-8) 

The existing test explicitly confirms the permanent-discard behavior: [10](#0-9) 

## Impact Explanation

**Permanent case**: If `NotifyErc20Added` fails with an unrecoverable error, the minter permanently ignores the token. Any ERC-20 deposits to the helper contract are silently missed — the minter never mints ckERC20, and the deposited tokens are permanently locked on Ethereum. The ledger and index canisters exist and consume cycles but are permanently idle. No governance action can recover the state without a code change, because a new `AddErc20Arg` proposal is rejected with `Erc20ContractAlreadyManaged`.

**Transient case**: During every normal token addition (spanning multiple timer cycles), `last_scraped_block_number` advances before the minter is notified. Deposits made during this window are permanently missed because the minter does not backfill.

This matches the allowed impact: **High — Significant Chain Fusion / ck-token security impact with concrete user fund loss**.

## Likelihood Explanation

**Transient**: Certain — every ckERC20 token addition passes through this intermediate state. The window is short (seconds to minutes) but real.

**Permanent**: Low but non-zero. Realistic triggers include a minter upgrade regression in `add_ckerc20_token` (the IC mainnet has already experienced a minter upgrade that broke client behavior, as documented in `minter_upgrade_2024_11_30.md`), a network mismatch, or a duplicate symbol/ledger ID collision. None of these require an unprivileged attacker — the failure mode is triggered by normal operational events (upgrades, governance proposals). [11](#0-10) 

## Recommendation

1. **Make `NotifyErc20Added` always recoverable**: In `is_recoverable`, treat `InterCanisterCallError` as always recoverable when the task is `NotifyErc20Added`, so it retries indefinitely until explicitly cancelled by governance.
2. **Add a governance-callable re-notification endpoint**: Expose an NNS-callable endpoint on the LSO that re-schedules `NotifyErc20Added` for a specific token, enabling recovery without a full re-addition.
3. **Track the Ethereum block number at proposal execution**: When notifying the minter, pass the Ethereum block number at which the NNS proposal was executed as the starting scrape point for the new token, preventing missed deposits during the intermediate state window.

## Proof of Concept

1. NNS executes a proposal to add ckERC20 token X.
2. LSO timer fires: creates ledger and index canisters, installs both wasms, schedules `Task::NotifyErc20Added`.
3. A minter upgrade is executed that introduces a regression in `add_ckerc20_token` (e.g., a new validation check that traps for token X's parameters).
4. LSO timer fires: `notify_erc20_added` calls `add_ckerc20_token` on the minter; minter traps → `CanisterError("trap: ERROR: ...")`.
5. `is_recoverable` returns `false` (message does not end with `"is stopped"` or `"is stopping"`).
6. `run_task` defuses the retry guard; task queue becomes empty.
7. User deposits token X to the Ethereum helper contract.
8. `ReceivedErc20LogScraping::next_scrape` does not include token X's address (not in `ckerc20_tokens`); `last_scraped_block_number` advances past the deposit block.
9. A new `AddErc20Arg` NNS proposal for token X fails with `Erc20ContractAlreadyManaged`.
10. User's ERC-20 tokens are permanently locked on Ethereum.

Reproducible as a unit test by extending `should_not_reschedule_failed_task_with_irrecoverable_error` to assert that a subsequent `AddErc20Arg` call returns `Erc20ContractAlreadyManaged` and that the minter's `ckerc20_tokens` map remains empty. [10](#0-9)

### Citations

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L212-220)
```rust
            } else {
                let _task_type = ScopeGuard::into_inner(rerun_task_guard);
                log!(
                    INFO,
                    "ERROR: task {:?} failed with unrecoverable error: {:?}. Task is discarded.",
                    task,
                    e
                );
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

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L624-624)
```rust
            TaskError::InterCanisterCallError(e) => is_recoverable(e),
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

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L878-890)
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
    Ok(())
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L1141-1144)
```rust
            runtime
                .call_canister(*minter_id, "add_ckerc20_token", args)
                .await
                .map_err(TaskError::InterCanisterCallError)
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

**File:** rs/ethereum/cketh/minter/src/eth_logs/scraping.rs (L70-91)
```rust
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

**File:** rs/ethereum/cketh/mainnet/minter_upgrade_2024_11_30.md (L17-23)
```markdown
## Motivation

Fix an undesired breaking changed introduced by proposal [134264](https://dashboard.internetcomputer.org/proposal/134264) :

1. The fields `eth_helper_contract_address` and `erc20_helper_contract_address` in `get_minter_info` were wrongly reused to point to the new helper smart contract [0x18901044688D3756C35Ed2b36D93e6a5B8e00E68](https://etherscan.io/address/0x18901044688D3756C35Ed2b36D93e6a5B8e00E68) that supports deposit with subaccounts and that was added as part of proposal [134264](https://dashboard.internetcomputer.org/proposal/134264).
2. This broke clients that relied on that information to make deposit of ETH or ERC-20 because the new helper smart contract has a different ABI. This is visible by such a [transaction](https://etherscan.io/tx/0x0968b25814221719bf966cf4bbd2de8290ed2ab42c049d451d64e46812d1574e), where the transaction tried to call the method `deposit` (`0xb214faa5`) that does exist on the [deprecated ETH helper smart contract](https://etherscan.io/address/0x7574eB42cA208A4f6960ECCAfDF186D627dCC175) but doesn't on the new contract (it should have been `depositEth` (`0x17c819c4`)).
3. The fix simply consists in reverting the changes regarding the values of the fields `eth_helper_contract_address` and `erc20_helper_contract_address` in `get_minter_info` (so that they point back to [0x7574eB42cA208A4f6960ECCAfDF186D627dCC175](https://etherscan.io/address/0x7574eB42cA208A4f6960ECCAfDF186D627dCC175) and [0x6abDA0438307733FC299e9C229FD3cc074bD8cC0](https://etherscan.io/address/0x6abDA0438307733FC299e9C229FD3cc074bD8cC0), respectively) and adding new fields to contain the state of the log scraping (address and last scraped block number) for the new helper smart contract.
```
