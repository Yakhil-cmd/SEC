### Title
Missing `last_eth_scraped_block_number` in `UpgradeArg` Causes Permanent Deposit Loss When ETH Helper Contract Address Is Replaced - (`rs/ethereum/cketh/minter/src/lifecycle/upgrade.rs`)

---

### Summary

The ckETH minter's `UpgradeArg` struct allows updating the ETH helper contract address (`ethereum_contract_address`), the ERC-20 helper contract address (`erc20_helper_contract_address`), and the deposit-with-subaccount helper contract address (`deposit_with_subaccount_helper_contract_address`). For the ERC-20 and deposit-with-subaccount helpers, corresponding `last_erc20_scraped_block_number` and `last_deposit_with_subaccount_scraped_block_number` fields exist so the operator can reset the scraping cursor to the new contract's deployment block. However, **no analogous `last_eth_scraped_block_number` field exists** for the ETH helper contract. When the ETH helper contract address is replaced via an NNS upgrade proposal, the minter's `last_scraped_block_number` for `LogScrapingId::EthDepositWithoutSubaccount` is not reset, causing all deposit events emitted by the new contract between its deployment block and the current scraping cursor to be **permanently skipped**, resulting in users' ETH deposits never being minted as ckETH.

---

### Finding Description

The `UpgradeArg` struct in `rs/ethereum/cketh/minter/src/lifecycle/upgrade.rs` exposes the following fields:

```rust
pub ethereum_contract_address: Option<String>,          // ETH helper — NO reset field
pub erc20_helper_contract_address: Option<String>,      // ERC-20 helper
pub last_erc20_scraped_block_number: Option<Nat>,       // ERC-20 reset field ✓
pub deposit_with_subaccount_helper_contract_address: Option<String>,
pub last_deposit_with_subaccount_scraped_block_number: Option<Nat>, // reset field ✓
``` [1](#0-0) 

When `post_upgrade` is called, the state is first rebuilt by replaying all stored events, then the `UpgradeArg` is applied via `process_event(s, EventType::Upgrade(args))`: [2](#0-1) 

Inside `State::upgrade()`, updating `ethereum_contract_address` only calls `set_contract_address` on the `EthDepositWithoutSubaccount` scraping slot — it does **not** touch `last_scraped_block_number`: [3](#0-2) 

`LogScrapingState::set_contract_address` simply overwrites the address field and leaves `last_scraped_block_number` unchanged: [4](#0-3) 

The `ReceivedEthLogScraping` implementation uses the stored `last_scraped_block_number` as the starting point for every subsequent scrape of the ETH helper contract: [5](#0-4) 

Because there is no `last_eth_scraped_block_number` field in `UpgradeArg`, the operator has **no mechanism** to reset the ETH scraping cursor when replacing the contract address. The ERC-20 and deposit-with-subaccount helpers are not affected because their upgrade args include the corresponding reset fields. [6](#0-5) 

---

### Impact Explanation

**Vulnerability class:** Chain-fusion mint/burn/replay bug — deposit events permanently skipped.

**Concrete scenario:**

1. ETH helper contract A (`0xAAAA`) is deployed at Ethereum block 18,000,000. The minter is initialized with `last_scraped_block_number = 18,000,000`.
2. Normal operation proceeds; the minter scrapes up to block 21,000,000. The `last_scraped_block_number` for `EthDepositWithoutSubaccount` is now 21,000,000.
3. A new ETH helper contract B (`0xBBBB`) is deployed at block 20,500,000.
4. An NNS governance proposal upgrades the minter with `ethereum_contract_address = "0xBBBB"`. No `last_eth_scraped_block_number` field exists to reset the cursor.
5. After upgrade, the minter begins scraping contract B from block 21,000,001 onward.
6. All `ReceivedEth` events emitted by contract B between blocks 20,500,000 and 21,000,000 are **permanently skipped**. Users who deposited ETH to contract B in that window never receive ckETH — their ETH is irretrievably lost from the minter's perspective.

The `EthDepositWithoutSubaccount` scraping is currently marked `Deprecated` but is still actively scraped whenever a contract address is set: [7](#0-6) 

The `ethereum_contract_address` field in `UpgradeArg` and the `cketh_minter.did` interface remain fully exposed and usable: [8](#0-7) 

---

### Likelihood Explanation

Replacing the ETH helper contract address requires an NNS governance proposal, which is a privileged but realistic operational action (it has been done on mainnet multiple times, as documented in the upgrade proposal history). The operator would naturally supply the new contract address but has no field available to also reset the scraping cursor — the asymmetry with ERC-20 and deposit-with-subaccount makes this an easy mistake to overlook. The real-world precedent of a breaking change caused by exactly this kind of address-update oversight is documented in the mainnet upgrade proposal `minter_upgrade_2024_11_30.md`, where updating helper contract address fields broke clients. [9](#0-8) 

---

### Recommendation

Add a `last_eth_scraped_block_number: Option<Nat>` field to `UpgradeArg` (mirroring the existing `last_erc20_scraped_block_number` and `last_deposit_with_subaccount_scraped_block_number` fields), and handle it in `State::upgrade()`:

```rust
// In UpgradeArg:
pub last_eth_scraped_block_number: Option<Nat>,

// In State::upgrade():
if let Some(block_number) = last_eth_scraped_block_number {
    self.log_scrapings.set_last_scraped_block_number(
        LogScrapingId::EthDepositWithoutSubaccount,
        BlockNumber::try_from(block_number)...,
    );
}
```

Additionally, enforce at the upgrade validation layer that whenever `ethereum_contract_address` is set, `last_eth_scraped_block_number` must also be provided, to prevent silent cursor mismatches.

---

### Proof of Concept

1. Deploy a fresh ckETH minter with `ethereum_contract_address = "0xAAAA"`, `last_scraped_block_number = 1000`.
2. Simulate scraping: advance `last_scraped_block_number` for `EthDepositWithoutSubaccount` to block 5000 via `SyncedDepositToBlock` events.
3. Upgrade the minter with `UpgradeArg { ethereum_contract_address: Some("0xBBBB"), ..Default::default() }`.
4. Observe via `get_minter_info()` that `eth_helper_contract_address` is now `0xBBBB` but `last_eth_scraped_block_number` is still 5000.
5. Inject synthetic `ReceivedEth` log events from contract `0xBBBB` at blocks 3000–4999 (simulating deposits that occurred before the upgrade).
6. Confirm the minter never processes those events — `events_to_mint` remains empty for those deposits — because the scraping cursor starts at 5001 and never looks back. [10](#0-9) [3](#0-2)

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

**File:** rs/ethereum/cketh/minter/src/lifecycle/upgrade.rs (L35-43)
```rust
pub fn post_upgrade(upgrade_args: Option<UpgradeArg>) {
    let start = ic_cdk::api::instruction_counter();

    STATE.with(|cell| {
        *cell.borrow_mut() = Some(replay_events());
    });
    if let Some(args) = upgrade_args {
        mutate_state(|s| process_event(s, EventType::Upgrade(args)))
    }
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L476-488)
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
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L502-527)
```rust
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
```

**File:** rs/ethereum/cketh/minter/src/state/eth_logs_scraping/mod.rs (L34-44)
```rust
    pub fn set_contract_address(
        &mut self,
        id: LogScrapingId,
        contract_address: Address,
    ) -> Result<(), LogScrapingStateError> {
        self.get_mut(id).set_contract_address(contract_address)
    }

    pub fn set_last_scraped_block_number(&mut self, id: LogScrapingId, block_number: BlockNumber) {
        self.get_mut(id).set_last_scraped_block_number(block_number)
    }
```

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

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L121-123)
```text
    // Change the ETH helper smart contract address.
    ethereum_contract_address : opt text;

```

**File:** rs/ethereum/cketh/mainnet/minter_upgrade_2024_11_30.md (L19-23)
```markdown
Fix an undesired breaking changed introduced by proposal [134264](https://dashboard.internetcomputer.org/proposal/134264) :

1. The fields `eth_helper_contract_address` and `erc20_helper_contract_address` in `get_minter_info` were wrongly reused to point to the new helper smart contract [0x18901044688D3756C35Ed2b36D93e6a5B8e00E68](https://etherscan.io/address/0x18901044688D3756C35Ed2b36D93e6a5B8e00E68) that supports deposit with subaccounts and that was added as part of proposal [134264](https://dashboard.internetcomputer.org/proposal/134264).
2. This broke clients that relied on that information to make deposit of ETH or ERC-20 because the new helper smart contract has a different ABI. This is visible by such a [transaction](https://etherscan.io/tx/0x0968b25814221719bf966cf4bbd2de8290ed2ab42c049d451d64e46812d1574e), where the transaction tried to call the method `deposit` (`0xb214faa5`) that does exist on the [deprecated ETH helper smart contract](https://etherscan.io/address/0x7574eB42cA208A4f6960ECCAfDF186D627dCC175) but doesn't on the new contract (it should have been `depositEth` (`0x17c819c4`)).
3. The fix simply consists in reverting the changes regarding the values of the fields `eth_helper_contract_address` and `erc20_helper_contract_address` in `get_minter_info` (so that they point back to [0x7574eB42cA208A4f6960ECCAfDF186D627dCC175](https://etherscan.io/address/0x7574eB42cA208A4f6960ECCAfDF186D627dCC175) and [0x6abDA0438307733FC299e9C229FD3cc074bD8cC0](https://etherscan.io/address/0x6abDA0438307733FC299e9C229FD3cc074bD8cC0), respectively) and adding new fields to contain the state of the log scraping (address and last scraped block number) for the new helper smart contract.
```
