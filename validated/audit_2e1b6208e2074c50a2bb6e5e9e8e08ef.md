### Title
ckETH/ckERC20 Minter `UpgradeArg` Allows Independent Updates of Logically Coupled Log-Scraping Configuration, Enabling Permanent Deposit Event Loss — (File: `rs/ethereum/cketh/minter/src/state.rs`)

---

### Summary

The ckETH minter's `UpgradeArg` exposes `erc20_helper_contract_address` and `last_erc20_scraped_block_number` (and the analogous pair `deposit_with_subaccount_helper_contract_address` / `last_deposit_with_subaccount_scraped_block_number`) as independent optional fields. These pairs are logically coupled: the contract address determines *which* Ethereum contract to watch, and the block number determines *from where* to start watching. Because `State::upgrade()` applies each field independently, an NNS upgrade proposal that sets only one of the two fields produces an inconsistent scraping state. The result is that ERC-20 deposit events emitted between the old and new block-number cursor are permanently skipped, and users who sent ERC-20 tokens to the helper contract in that window never receive their ckERC20 tokens.

---

### Finding Description

**Root cause — `State::upgrade()` applies the two fields independently:** [1](#0-0) 

```rust
if let Some(address) = erc20_helper_contract_address {
    self.log_scrapings
        .set_contract_address(LogScrapingId::Erc20DepositWithoutSubaccount, …)?;
}
if let Some(block_number) = last_erc20_scraped_block_number {
    self.log_scrapings.set_last_scraped_block_number(
        LogScrapingId::Erc20DepositWithoutSubaccount, …);
}
```

The Candid interface exposes both as separate `opt` fields: [2](#0-1) 

`set_last_scraped_block_number` accepts any value with no lower-bound guard: [3](#0-2) 

The scraping loop uses `last_scraped_block_number + 1` as its exclusive lower bound, so any block below the cursor is permanently unreachable: [4](#0-3) 

The same structural problem exists for the `deposit_with_subaccount` pair: [5](#0-4) 

---

### Impact Explanation

**Scenario A — block-number advanced without changing the contract address:**

An NNS upgrade proposal sets `last_erc20_scraped_block_number = N + K` while leaving `erc20_helper_contract_address` unchanged. The minter immediately resumes scraping from block `N + K + 1`. Every ERC-20 `ReceivedErc20` event emitted by the helper contract between blocks `N + 1` and `N + K` is permanently skipped. Users who called the helper contract in that window sent real ERC-20 tokens on Ethereum but will never receive ckERC20 on the IC. The tokens are locked in the helper contract with no recovery path inside the minter.

**Scenario B — contract address changed without resetting the block cursor:**

An NNS upgrade proposal sets `erc20_helper_contract_address = 0xNEW` (deployed at Ethereum block `M`) while leaving `last_erc20_scraped_block_number = N` where `N > M`. The minter begins scraping 0xNEW from block `N + 1`, permanently missing all deposits made to 0xNEW between its deployment block `M` and block `N`.

Both scenarios result in a **chain-fusion mint omission**: real ERC-20 value is transferred on Ethereum but the corresponding ckERC20 mint never occurs on the IC.

---

### Likelihood Explanation

The ckETH minter is controlled by the NNS. Any NNS upgrade proposal that supplies only one of the two coupled fields — a realistic operational mistake, as evidenced by the real-world incident documented in `minter_upgrade_2024_11_30.md` where a proposal incorrectly reused existing fields — produces the inconsistent state. The upgrade args are Candid-encoded binary blobs reviewed under time pressure; omitting one optional field is easy to miss. The historical record shows this class of misconfiguration has already occurred in production. [6](#0-5) 

---

### Recommendation

**Short term:** Add a validation step inside `State::upgrade()` that rejects an `UpgradeArg` where `erc20_helper_contract_address` is `Some` but `last_erc20_scraped_block_number` is `None` (and vice versa for the subaccount pair). Alternatively, group the two fields into a single struct so they must always be supplied together.

**Long term:** Enforce a monotonicity invariant on `last_erc20_scraped_block_number`: reject any upgrade that would set it to a value lower than or equal to the current cursor, and require that when a new contract address is supplied the block number is also supplied and is ≥ the contract's deployment block.

---

### Proof of Concept

```
State before upgrade:
  erc20_helper_contract_address  = 0x6abDA0438307733FC299e9C229FD3cc074bD8cC0
  last_erc20_scraped_block_number = 19_900_000

NNS upgrade proposal UpgradeArg:
  last_erc20_scraped_block_number = opt 20_000_000   // ← only this field set
  erc20_helper_contract_address   = null             // ← not changed

State after upgrade:
  erc20_helper_contract_address  = 0x6abDA0438307733FC299e9C229FD3cc074bD8cC0
  last_erc20_scraped_block_number = 20_000_000       // cursor jumped forward

Effect:
  scrape_logs() calls scrape_until_block::<ReceivedErc20LogScraping>()
  next block range starts at 20_000_001
  All ReceivedErc20 events in blocks 19_900_001–20_000_000 are permanently skipped.
  Users who deposited ERC-20 in that window receive no ckERC20 and cannot recover funds.
``` [7](#0-6) [8](#0-7)

### Citations

**File:** rs/ethereum/cketh/minter/src/state.rs (L489-508)
```rust
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
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L510-527)
```rust
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

**File:** rs/ethereum/cketh/minter/src/state/eth_logs_scraping/mod.rs (L175-177)
```rust
    pub fn set_last_scraped_block_number(&mut self, block_number: BlockNumber) {
        self.last_scraped_block_number = block_number;
    }
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L156-158)
```rust
    scrape_until_block::<ReceivedEthLogScraping>(last_block_number, max_block_spread).await;
    scrape_until_block::<ReceivedErc20LogScraping>(last_block_number, max_block_spread).await;
    scrape_until_block::<ReceivedEthOrErc20LogScraping>(last_block_number, max_block_spread).await;
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L200-206)
```rust
    let block_range = BlockRangeInclusive::new(
        scrape
            .last_scraped_block_number
            .checked_increment()
            .unwrap_or(BlockNumber::MAX),
        last_block_number,
    );
```

**File:** rs/ethereum/cketh/mainnet/minter_upgrade_2024_11_30.md (L19-23)
```markdown
Fix an undesired breaking changed introduced by proposal [134264](https://dashboard.internetcomputer.org/proposal/134264) :

1. The fields `eth_helper_contract_address` and `erc20_helper_contract_address` in `get_minter_info` were wrongly reused to point to the new helper smart contract [0x18901044688D3756C35Ed2b36D93e6a5B8e00E68](https://etherscan.io/address/0x18901044688D3756C35Ed2b36D93e6a5B8e00E68) that supports deposit with subaccounts and that was added as part of proposal [134264](https://dashboard.internetcomputer.org/proposal/134264).
2. This broke clients that relied on that information to make deposit of ETH or ERC-20 because the new helper smart contract has a different ABI. This is visible by such a [transaction](https://etherscan.io/tx/0x0968b25814221719bf966cf4bbd2de8290ed2ab42c049d451d64e46812d1574e), where the transaction tried to call the method `deposit` (`0xb214faa5`) that does exist on the [deprecated ETH helper smart contract](https://etherscan.io/address/0x7574eB42cA208A4f6960ECCAfDF186D627dCC175) but doesn't on the new contract (it should have been `depositEth` (`0x17c819c4`)).
3. The fix simply consists in reverting the changes regarding the values of the fields `eth_helper_contract_address` and `erc20_helper_contract_address` in `get_minter_info` (so that they point back to [0x7574eB42cA208A4f6960ECCAfDF186D627dCC175](https://etherscan.io/address/0x7574eB42cA208A4f6960ECCAfDF186D627dCC175) and [0x6abDA0438307733FC299e9C229FD3cc074bD8cC0](https://etherscan.io/address/0x6abDA0438307733FC299e9C229FD3cc074bD8cC0), respectively) and adding new fields to contain the state of the log scraping (address and last scraped block number) for the new helper smart contract.
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
