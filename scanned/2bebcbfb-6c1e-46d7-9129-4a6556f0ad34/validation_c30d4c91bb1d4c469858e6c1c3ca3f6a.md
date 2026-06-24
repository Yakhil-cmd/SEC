### Title
Missing `last_eth_scraped_block_number` Update Path in ckETH Minter `UpgradeArg` While Analogous ERC-20 and Subaccount Fields Are Updatable - (File: `rs/ethereum/cketh/minter/src/lifecycle/upgrade.rs`)

---

### Summary

The ckETH minter's `UpgradeArg` exposes `last_erc20_scraped_block_number` and `last_deposit_with_subaccount_scraped_block_number` as updatable fields, allowing NNS governance to reset the scraping cursor for those two log-scraping paths. The analogous field for the primary ETH deposit path (`LogScrapingId::EthDepositWithoutSubaccount`) — `last_eth_scraped_block_number` — has no corresponding field in `UpgradeArg` and therefore cannot be updated post-deployment. This is a direct analog of the reported M-03 pattern: all peer config fields have an update mechanism, but one does not.

---

### Finding Description

The ckETH minter tracks three independent log-scraping cursors, one per `LogScrapingId`:

| `LogScrapingId` | Contract address updatable via upgrade? | Last scraped block updatable via upgrade? |
|---|---|---|
| `EthDepositWithoutSubaccount` | Yes (`ethereum_contract_address`) | **No** |
| `Erc20DepositWithoutSubaccount` | Yes (`erc20_helper_contract_address`) | Yes (`last_erc20_scraped_block_number`) |
| `EthOrErc20DepositWithSubaccount` | Yes (`deposit_with_subaccount_helper_contract_address`) | Yes (`last_deposit_with_subaccount_scraped_block_number`) |

The `UpgradeArg` struct in `rs/ethereum/cketh/minter/src/lifecycle/upgrade.rs` contains: [1](#0-0) 

The `upgrade` method in `rs/ethereum/cketh/minter/src/state.rs` handles `last_erc20_scraped_block_number` and `last_deposit_with_subaccount_scraped_block_number` but has no branch for the ETH path's last scraped block: [2](#0-1) 

The ETH scraping cursor is only ever advanced automatically by the minter timer via `EventType::SyncedToBlock`, which calls `set_last_scraped_block_number(LogScrapingId::EthDepositWithoutSubaccount, ...)`: [3](#0-2) 

The `LogScrapings` struct exposes `set_last_scraped_block_number` for all IDs uniformly, so the capability exists at the state layer: [4](#0-3) 

The `EthDepositWithoutSubaccount` path is still functionally active whenever a contract address is set — the `Deprecated` label is cosmetic only and does not suppress scraping: [5](#0-4) 

The `InitArg` sets `last_scraped_block_number` for the ETH path at deployment time: [6](#0-5) 

---

### Impact Explanation

If the ETH helper contract address is rotated via `ethereum_contract_address` in `UpgradeArg` (which is supported), the minter will resume scraping from the old cursor value. If the new contract was deployed at a block number higher than the current cursor, the minter will scan a large range of irrelevant blocks before reaching the new contract's events — wasting HTTP outcall budget and delaying deposit processing.

More critically: if a bug causes the minter to skip or incorrectly process a range of ETH deposit events, NNS governance has no mechanism to rewind the ETH scraping cursor to re-process those blocks. The ERC-20 and subaccount paths both have this recovery capability. Without it, missed ETH deposits on the `EthDepositWithoutSubaccount` path become permanently unrecoverable without a full canister reinstall (which would destroy all minter state).

---

### Likelihood Explanation

Medium. The ETH helper contract address has already been changed in production (mainnet upgrade proposals reference rotating `ethereum_contract_address`). Any future contract rotation, or any bug causing missed ETH deposit events, would expose this gap. The NNS governance path that would normally invoke `UpgradeArg` is the standard, unprivileged-proposal-driven path available to any NNS neuron holder.

---

### Recommendation

Add `last_eth_scraped_block_number: Option<Nat>` to `UpgradeArg` and handle it in `State::upgrade`, mirroring the existing ERC-20 and subaccount branches:

```rust
// In UpgradeArg (rs/ethereum/cketh/minter/src/lifecycle/upgrade.rs)
pub last_eth_scraped_block_number: Option<Nat>,

// In State::upgrade (rs/ethereum/cketh/minter/src/state.rs)
if let Some(block_number) = last_eth_scraped_block_number {
    self.log_scrapings.set_last_scraped_block_number(
        LogScrapingId::EthDepositWithoutSubaccount,
        BlockNumber::try_from(block_number).map_err(|e| {
            InvalidStateError::InvalidLastScrapedBlockNumber(format!("ERROR: {e}"))
        })?,
    );
}
```

Also add the field to the Candid interface `rs/ethereum/cketh/minter/cketh_minter.did` under `UpgradeArg`.

---

### Proof of Concept

1. Deploy ckETH minter with `last_scraped_block_number = N` and `ethereum_contract_address = A`.
2. Minter scrapes up to block `N + k` for contract `A`.
3. Submit NNS upgrade proposal with `UpgradeArg { ethereum_contract_address = Some(B), last_erc20_scraped_block_number = Some(N + k), ... }` — the ERC-20 cursor is reset correctly.
4. Attempt to also reset the ETH cursor to `N + k` for the new contract `B` — no field exists in `UpgradeArg` to do so. The ETH cursor remains at `N + k` from the old contract, which is correct by coincidence here, but cannot be independently controlled.
5. Simulate a missed-deposit scenario: manually set `last_eth_scraped_block_number` to `N + k + 100` in state (simulating a forward skip bug). There is no upgrade path to rewind it to `N + k`, unlike the ERC-20 path where `last_erc20_scraped_block_number = Some(N + k)` in `UpgradeArg` would recover the situation. [1](#0-0) [7](#0-6) [8](#0-7)

### Citations

**File:** rs/ethereum/cketh/minter/src/lifecycle/upgrade.rs (L11-33)
```rust
#[derive(Clone, Eq, PartialEq, Debug, Default, CandidType, Decode, Deserialize, Encode)]
pub struct UpgradeArg {
    #[cbor(n(0), with = "icrc_cbor::nat::option")]
    pub next_transaction_nonce: Option<Nat>,
    #[cbor(n(1), with = "icrc_cbor::nat::option")]
    pub minimum_withdrawal_amount: Option<Nat>,
    #[n(2)]
    pub ethereum_contract_address: Option<String>,
    #[n(3)]
    pub ethereum_block_height: Option<CandidBlockTag>,
    #[cbor(n(4), with = "icrc_cbor::principal::option")]
    pub ledger_suite_orchestrator_id: Option<Principal>,
    #[n(5)]
    pub erc20_helper_contract_address: Option<String>,
    #[cbor(n(6), with = "icrc_cbor::nat::option")]
    pub last_erc20_scraped_block_number: Option<Nat>,
    #[cbor(n(7), with = "icrc_cbor::principal::option")]
    pub evm_rpc_id: Option<Principal>,
    #[n(8)]
    pub deposit_with_subaccount_helper_contract_address: Option<String>,
    #[cbor(n(9), with = "icrc_cbor::nat::option")]
    pub last_deposit_with_subaccount_scraped_block_number: Option<Nat>,
}
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L450-538)
```rust
    fn upgrade(&mut self, upgrade_args: UpgradeArg) -> Result<(), InvalidStateError> {
        use std::str::FromStr;

        let UpgradeArg {
            next_transaction_nonce,
            minimum_withdrawal_amount,
            ethereum_contract_address,
            ethereum_block_height,
            ledger_suite_orchestrator_id,
            erc20_helper_contract_address,
            last_erc20_scraped_block_number,
            evm_rpc_id,
            deposit_with_subaccount_helper_contract_address,
            last_deposit_with_subaccount_scraped_block_number,
        } = upgrade_args;
        if let Some(nonce) = next_transaction_nonce {
            let nonce = TransactionNonce::try_from(nonce)
                .map_err(|e| InvalidStateError::InvalidTransactionNonce(format!("ERROR: {e}")))?;
            self.eth_transactions.update_next_transaction_nonce(nonce);
        }
        if let Some(amount) = minimum_withdrawal_amount {
            let minimum_withdrawal_amount = Wei::try_from(amount).map_err(|e| {
                InvalidStateError::InvalidMinimumWithdrawalAmount(format!("ERROR: {e}"))
            })?;
            self.cketh_minimum_withdrawal_amount = minimum_withdrawal_amount;
        }
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
        if let Some(block_number) = last_deposit_with_subaccount_scraped_block_number {
            self.log_scrapings.set_last_scraped_block_number(
                LogScrapingId::EthOrErc20DepositWithSubaccount,
                BlockNumber::try_from(block_number).map_err(|e| {
                    InvalidStateError::InvalidLastErc20ScrapedBlockNumber(format!("ERROR: {e}"))
                })?,
            );
        }
        if let Some(block_height) = ethereum_block_height {
            self.ethereum_block_height = block_height;
        }
        if let Some(orchestrator_id) = ledger_suite_orchestrator_id {
            self.ledger_suite_orchestrator_id = Some(orchestrator_id);
        }
        if let Some(evm_id) = evm_rpc_id {
            self.evm_rpc_id = evm_id;
        }
        self.validate_config()
    }
```

**File:** rs/ethereum/cketh/minter/src/state/audit.rs (L61-66)
```rust
        EventType::SyncedToBlock { block_number } => {
            state.log_scrapings.set_last_scraped_block_number(
                LogScrapingId::EthDepositWithoutSubaccount,
                *block_number,
            );
        }
```

**File:** rs/ethereum/cketh/minter/src/state/eth_logs_scraping/mod.rs (L42-44)
```rust
    pub fn set_last_scraped_block_number(&mut self, id: LogScrapingId, block_number: BlockNumber) {
        self.get_mut(id).set_last_scraped_block_number(block_number)
    }
```

**File:** rs/ethereum/cketh/minter/src/state/eth_logs_scraping/mod.rs (L91-107)
```rust
#[derive(Clone, PartialEq, Copy, Debug, PartialOrd, Ord, Eq, EnumIter)]
#[repr(u8)]
pub enum LogScrapingId {
    EthDepositWithoutSubaccount,
    Erc20DepositWithoutSubaccount,
    EthOrErc20DepositWithSubaccount,
}

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

**File:** rs/ethereum/cketh/minter/src/eth_logs/scraping.rs (L48-62)
```rust
impl LogScraping for ReceivedEthLogScraping {
    const ID: LogScrapingId = LogScrapingId::EthDepositWithoutSubaccount;
    type Parser = ReceivedEthLogParser;

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

**File:** rs/ethereum/cketh/minter/src/lifecycle/init.rs (L64-85)
```rust
        let last_scraped_block_number = BlockNumber::try_from(last_scraped_block_number)
            .map_err(|e| InvalidStateError::InvalidLastScrapedBlockNumber(format!("ERROR: {e}")))?;
        let first_scraped_block_number =
            last_scraped_block_number
                .checked_increment()
                .ok_or_else(|| {
                    InvalidStateError::InvalidLastScrapedBlockNumber(
                        "ERROR: last_scraped_block_number is at maximum value".to_string(),
                    )
                })?;
        let evm_rpc_id = evm_rpc_id.unwrap_or(match ethereum_network {
            EthereumNetwork::Mainnet => EVM_RPC_ID_PRODUCTION,
            EthereumNetwork::Sepolia => EVM_RPC_ID_STAGING,
        });
        let mut log_scrapings = LogScrapings::new(last_scraped_block_number);
        if let Some(contract_address) = eth_helper_contract_address {
            log_scrapings
                .set_contract_address(LogScrapingId::EthDepositWithoutSubaccount, contract_address)
                .map_err(|e| {
                    InvalidStateError::InvalidEthereumContractAddress(format!("ERROR: {e:?}"))
                })?;
        }
```
