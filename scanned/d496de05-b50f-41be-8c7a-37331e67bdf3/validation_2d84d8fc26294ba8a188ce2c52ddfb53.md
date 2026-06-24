### Title
Compromised Helper Contract Address Cannot Be Removed from ckETH/ckERC20 Minter — (`File: rs/ethereum/cketh/minter/src/state/eth_logs_scraping/mod.rs`)

---

### Summary

The ckETH minter's `UpgradeArg` allows the NNS/admin to set Ethereum helper contract addresses for log scraping. However, `LogScrapingState::set_contract_address` explicitly rejects `Address::ZERO`, and there is no mechanism to clear (`None`) a previously set contract address. Once a helper contract address is registered, it cannot be removed or nullified via upgrade — only replaced with another non-zero address. This is the direct IC analog of the reported SpokePool `_setOftMessenger` vulnerability.

---

### Finding Description

The ckETH minter stores three Ethereum helper contract addresses in `LogScrapings`, one per `LogScrapingId`:

- `EthDepositWithoutSubaccount`
- `Erc20DepositWithoutSubaccount`
- `EthOrErc20DepositWithSubaccount`

Each is stored as `Option<Address>` in `LogScrapingState`. The only mutation path available via `UpgradeArg` is `set_contract_address`, which:

1. Rejects `Address::ZERO` with an error.
2. Sets `self.contract_address = Some(contract_address)` — it can only set, never clear. [1](#0-0) 

The `upgrade()` function in `State` processes the three address fields from `UpgradeArg` by calling `set_contract_address` on each. There is no branch that sets `contract_address` back to `None`. [2](#0-1) 

The `UpgradeArg` Candid interface exposes these as `opt text` fields. Passing `null` (i.e., `None`) simply skips the update — it does not clear the stored address. Passing `"0x0000000000000000000000000000000000000000"` is explicitly rejected. [3](#0-2) 

The `scrape_logs()` function calls `next_scrape()` for each scraping type, which returns `Some(Scrape)` whenever a contract address is set, causing the minter to continue making HTTPS outcalls to the EVM RPC canister for that address. [4](#0-3) [5](#0-4) 

---

### Impact Explanation

If a registered Ethereum helper contract address becomes compromised (e.g., the Ethereum contract is exploited, its ownership is taken over, or it begins emitting malicious `ReceivedEth`/`ReceivedErc20` events), the NNS **cannot stop the minter from scraping it** without replacing it with a different non-zero address. There is no emergency "disable" path.

The minter processes scraped log events and mints ckETH/ckERC20 tokens based on them: [6](#0-5) 

A compromised helper contract that emits fraudulent `ReceivedEth` or `ReceivedErc20` events would cause the minter to mint unbacked ckETH or ckERC20 tokens, breaking the 1:1 peg and draining the minter's ETH/ERC20 reserves. The NNS governance process (which requires a proposal, voting period, and execution) introduces a multi-day delay before a replacement address can be set, during which the minter continues scraping the compromised contract.

---

### Likelihood Explanation

The `EthDepositWithoutSubaccount` and `Erc20DepositWithoutSubaccount` contracts are marked `Deprecated` in the current codebase but still scraped if their addresses are set. [7](#0-6) 

The active contract `EthOrErc20DepositWithSubaccount` (`0x18901044688D3756C35Ed2b36D93e6a5B8e00E68`) is an immutable Ethereum smart contract — its `minterAddress` is set at construction and cannot be changed. However, if a future helper contract is deployed with an upgradeable proxy pattern, or if the Ethereum contract's behavior is manipulated at the EVM level, the inability to remove the address from the minter becomes a critical gap. The likelihood is **medium**: the Ethereum contracts themselves are currently immutable, but the missing removal capability is a governance control gap that could matter in an emergency.

---

### Recommendation

1. Add a `clear_contract_address` method to `LogScrapingState` that sets `self.contract_address = None`.
2. Extend `UpgradeArg` with explicit boolean or sentinel fields (e.g., `clear_ethereum_contract_address: opt bool`) to allow the NNS to null out a registered address.
3. Alternatively, accept the string `"0x0000000000000000000000000000000000000000"` in `set_contract_address` as a special sentinel to clear the address, consistent with the fix applied in the referenced SpokePool PR #1034. [1](#0-0) 

---

### Proof of Concept

**Step 1**: NNS sets a helper contract address via upgrade proposal:
```
UpgradeArg { ethereum_contract_address: Some("0xCompromised...") }
```
This calls `set_contract_address(LogScrapingId::EthDepositWithoutSubaccount, 0xCompromised...)`, storing it.

**Step 2**: The Ethereum contract at `0xCompromised` begins emitting fraudulent `ReceivedEth` events.

**Step 3**: NNS attempts emergency removal. Passing `ethereum_contract_address: null` in `UpgradeArg` is a no-op (the `if let Some(address) = ethereum_contract_address` branch is skipped). [8](#0-7) 

**Step 4**: Passing `ethereum_contract_address: Some("0x0000000000000000000000000000000000000000")` is rejected:

```
LogScrapingStateError::InvalidContractAddress("contract address must not be zero")
``` [9](#0-8) 

**Step 5**: The minter continues calling `scrape_until_block::<ReceivedEthLogScraping>()` on every timer tick, processing fraudulent events and minting unbacked ckETH until a new non-zero replacement address is set via a full NNS governance proposal cycle (days of delay). [10](#0-9)

### Citations

**File:** rs/ethereum/cketh/minter/src/state/eth_logs_scraping/mod.rs (L99-107)
```rust
impl LogScrapingId {
    fn status(&self) -> LogScrapingStatus {
        match self {
            LogScrapingId::EthDepositWithoutSubaccount => LogScrapingStatus::Deprecated,
            LogScrapingId::Erc20DepositWithoutSubaccount => LogScrapingStatus::Deprecated,
            LogScrapingId::EthOrErc20DepositWithSubaccount => LogScrapingStatus::Active,
        }
    }
}
```

**File:** rs/ethereum/cketh/minter/src/state/eth_logs_scraping/mod.rs (L162-173)
```rust
    pub fn set_contract_address(
        &mut self,
        contract_address: Address,
    ) -> Result<(), LogScrapingStateError> {
        if contract_address == Address::ZERO {
            return Err(LogScrapingStateError::InvalidContractAddress(
                "contract address must not be zero".to_string(),
            ));
        }
        self.contract_address = Some(contract_address);
        Ok(())
    }
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L191-210)
```rust
    fn record_event_to_mint(&mut self, event: &ReceivedEvent) {
        let event_source = event.source();
        assert!(
            !self.events_to_mint.contains_key(&event_source),
            "there must be no two different events with the same source"
        );
        assert!(!self.minted_events.contains_key(&event_source));
        assert!(!self.invalid_events.contains_key(&event_source));
        if let ReceivedEvent::Erc20(event) = event {
            assert!(
                self.ckerc20_tokens
                    .contains_alt(&event.erc20_contract_address),
                "BUG: unsupported ERC-20 contract address in event {event:?}"
            )
        }

        self.events_to_mint.insert(event_source, event.clone());

        self.update_balance_upon_deposit(event)
    }
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L476-519)
```rust
        if let Some(address) = ethereum_contract_address {
            let eth_helper_contract_address = Address::from_str(&address).map_err(|e| {
                InvalidStateError::InvalidEthereumContractAddress(format!("ERROR: {e}"))
            })?;
            self.log_scrapings
                .set_contract_address(
                    LogScrapingId::EthDepositWithoutSubaccount,
                    eth_helper_contract_address,
                )
                .map_err(|e| {
                    InvalidStateError::InvalidEthereumContractAddress(format!("ERROR: {e:?}"))
                })?;
        }
        if let Some(address) = erc20_helper_contract_address {
            let erc20_helper_contract_address = Address::from_str(&address).map_err(|e| {
                InvalidStateError::InvalidErc20HelperContractAddress(format!("ERROR: {e}"))
            })?;
            self.log_scrapings
                .set_contract_address(
                    LogScrapingId::Erc20DepositWithoutSubaccount,
                    erc20_helper_contract_address,
                )
                .map_err(|e| {
                    InvalidStateError::InvalidEthereumContractAddress(format!("ERROR: {e:?}"))
                })?;
        }
        if let Some(block_number) = last_erc20_scraped_block_number {
            self.log_scrapings.set_last_scraped_block_number(
                LogScrapingId::Erc20DepositWithoutSubaccount,
                BlockNumber::try_from(block_number).map_err(|e| {
                    InvalidStateError::InvalidLastErc20ScrapedBlockNumber(format!("ERROR: {e}"))
                })?,
            );
        }
        if let Some(address) = deposit_with_subaccount_helper_contract_address {
            let address = Address::from_str(&address).map_err(|e| {
                InvalidStateError::InvalidErc20HelperContractAddress(format!("ERROR: {e}"))
            })?;
            self.log_scrapings
                .set_contract_address(LogScrapingId::EthOrErc20DepositWithSubaccount, address)
                .map_err(|e| {
                    InvalidStateError::InvalidEthereumContractAddress(format!("ERROR: {e:?}"))
                })?;
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

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L140-159)
```rust
pub async fn scrape_logs() {
    let _guard = match TimerGuard::new(TaskType::ScrapEthLogs) {
        Ok(guard) => guard,
        Err(_) => return,
    };
    let last_block_number = match update_last_observed_block_number().await {
        Some(block_number) => block_number,
        None => {
            log!(
                DEBUG,
                "[scrape_logs]: skipping scrapping logs: no last observed block number"
            );
            return;
        }
    };
    let max_block_spread = read_state(|s| s.max_block_spread_for_logs_scraping());
    scrape_until_block::<ReceivedEthLogScraping>(last_block_number, max_block_spread).await;
    scrape_until_block::<ReceivedErc20LogScraping>(last_block_number, max_block_spread).await;
    scrape_until_block::<ReceivedEthOrErc20LogScraping>(last_block_number, max_block_spread).await;
}
```

**File:** rs/ethereum/cketh/minter/src/eth_logs/scraping.rs (L52-62)
```rust
    fn next_scrape(state: &State) -> Option<Scrape> {
        let contract_address = *Self::contract_address(state)?;
        let last_scraped_block_number = Self::last_scraped_block_number(state);
        let topics = vec![Topic::Single(Hex32::from(RECEIVED_ETH_EVENT_TOPIC))];
        Some(Scrape {
            contract_address,
            last_scraped_block_number,
            topics,
        })
    }
}
```
