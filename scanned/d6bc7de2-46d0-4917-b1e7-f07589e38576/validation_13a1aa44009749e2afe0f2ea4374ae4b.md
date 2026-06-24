### Title
ckERC20 Token Support Cannot Be Removed from the ckETH Minter - (File: rs/ethereum/cketh/minter/src/main.rs)

### Summary
The ckETH minter canister exposes `add_ckerc20_token` to register new ckERC20 tokens, but provides no corresponding removal endpoint. Once a token is registered, it permanently remains in the minter's `ckerc20_tokens` state. If an underlying ERC-20 contract is compromised, deprecated, or must be delisted, the minter cannot stop minting ckERC20 tokens for new deposits or processing withdrawal requests for that token, mirroring the Ondo `SourceBridge`/`DestinationBridge` chain-support-cannot-be-cleared bug.

### Finding Description

The minter's only token-management endpoint is `add_ckerc20_token`: [1](#0-0) 

It calls `record_add_ckerc20_token`, which inserts into `ckerc20_tokens` (a `DedupMultiKeyMap`) with no removal path: [2](#0-1) 

The `EventType` enum has `AddedCkErc20Token` but no `RemovedCkErc20Token`: [3](#0-2) 

`apply_state_transition` handles `AddedCkErc20Token` but has no removal branch: [4](#0-3) 

The `UpgradeArg` struct (the only other configuration path) has no field for removing a ckERC20 token: [5](#0-4) 

The `DedupMultiKeyMap` itself supports `remove_entry`, but it is never called from any canister endpoint or upgrade path: [6](#0-5) 

The log-scraping logic for ERC-20 deposits uses `ckerc20_tokens` to build the topic filter, so a deprecated token's contract address remains in every scrape indefinitely: [7](#0-6) 

### Impact Explanation

**Uncontrollable minting of ckERC20 tokens for a compromised ERC-20 contract.** If the underlying ERC-20 contract is upgraded to allow infinite minting (or is otherwise compromised), an attacker can deposit arbitrary amounts to the helper contract. Because the minter cannot remove the token from `ckerc20_tokens`, it will continue to mint ckERC20 tokens for every such deposit, inflating the ckERC20 supply without a corresponding ERC-20 backing.

**Irreversible ckETH loss for users withdrawing a deprecated token.** The `withdraw_erc20` flow first burns ckETH for gas fees, then burns ckERC20 tokens: [8](#0-7) 

If the Ethereum transaction fails (e.g., the deprecated ERC-20 contract no longer accepts transfers), the ckERC20 tokens are reimbursed but the ckETH gas fee is not fully reimbursed (a penalty is deducted). Users who attempt to withdraw a deprecated ckERC20 token permanently lose ckETH with no recourse, because the minter cannot be configured to reject such requests for a specific token.

### Likelihood Explanation

ERC-20 contracts are upgradeable proxies in many cases (USDC, USDT, etc.). A contract compromise, regulatory delisting, or migration to a new contract address is a realistic operational event. The NNS has no mechanism to respond by removing the affected token from the minter short of a full minter reinstall (which would destroy all in-flight state). The `add_ckerc20_token` endpoint is already live on mainnet for ckUSDC and ckUSDT, making this a present operational risk.

### Recommendation

1. Add a `RemovedCkErc20Token` variant to `EventType` and a corresponding branch in `apply_state_transition` that calls `ckerc20_tokens.remove_entry(ledger_id)`.
2. Expose a `remove_ckerc20_token` update endpoint restricted to the orchestrator (mirroring `add_ckerc20_token`), so the NNS can delist a token via an orchestrator upgrade proposal.
3. In `withdraw_erc20`, check that the requested `ckerc20_ledger_id` is still in `ckerc20_tokens` after the ckETH burn succeeds but before the ckERC20 burn, and reject with a clear error if the token has been removed in the interim.

### Proof of Concept

1. NNS adds ckUSDC via an orchestrator upgrade proposal; `add_ckerc20_token` is called, inserting ckUSDC into `ckerc20_tokens`.
2. The USDC ERC-20 contract is compromised; the NNS wishes to delist ckUSDC.
3. There is no `remove_ckerc20_token` endpoint and no `UpgradeArg` field to remove the token. The only options are stopping the entire minter (halting all ckETH and ckERC20 operations) or a full reinstall (destroying all pending withdrawal state).
4. While the NNS deliberates, the attacker mints unlimited USDC on Ethereum, deposits to the helper contract, and the minter mints unlimited ckUSDC, which the attacker swaps for other assets on-chain.
5. Simultaneously, ordinary users calling `withdraw_erc20` for ckUSDC burn ckETH for gas fees; the resulting Ethereum transactions fail because the compromised contract rejects transfers; users lose ckETH permanently. [1](#0-0) [9](#0-8)

### Citations

**File:** rs/ethereum/cketh/minter/src/main.rs (L448-477)
```rust
    match cketh_ledger
        .burn_from(
            cketh_account,
            erc20_tx_fee,
            BurnMemo::Erc20GasFee {
                ckerc20_token_symbol: ckerc20_token.ckerc20_token_symbol.clone(),
                ckerc20_withdrawal_amount,
                to_address: destination,
            },
        )
        .await
    {
        Ok(cketh_ledger_burn_index) => {
            log!(
                INFO,
                "[withdraw_erc20]: burning {} {} from account {}",
                ckerc20_withdrawal_amount,
                ckerc20_token.ckerc20_token_symbol,
                ckerc20_account
            );
            match LedgerClient::ckerc20_ledger(&ckerc20_token)
                .burn_from(
                    ckerc20_account,
                    ckerc20_withdrawal_amount,
                    BurnMemo::Erc20Convert {
                        ckerc20_withdrawal_id: cketh_ledger_burn_index.get(),
                        to_address: destination,
                    },
                )
                .await
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

**File:** rs/ethereum/cketh/minter/src/state.rs (L98-103)
```rust
    /// ERC-20 tokens that the minter can mint:
    /// - primary key: ledger ID for the ckERC20 token
    /// - secondary key: ERC-20 contract address on Ethereum
    /// - value: ckERC20 token symbol
    pub ckerc20_tokens: DedupMultiKeyMap<Principal, Address, CkTokenSymbol>,
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

**File:** rs/ethereum/cketh/minter/src/state/event.rs (L100-103)
```rust
    /// Add a new ckERC20 token.
    #[n(14)]
    AddedCkErc20Token(#[n(0)] CkErc20Token),
    /// The minter discovered a ckERC20 deposit in the helper contract logs.
```

**File:** rs/ethereum/cketh/minter/src/state/audit.rs (L126-128)
```rust
        EventType::AddedCkErc20Token(ckerc20_token) => {
            state.record_add_ckerc20_token(ckerc20_token.clone());
        }
```

**File:** rs/ethereum/cketh/minter/src/map.rs (L158-170)
```rust
    pub fn remove_entry<Q>(&mut self, key: &Q) -> Option<(Key, AltKey, V)>
    where
        Key: Borrow<Q>,
        Q: ?Sized + Ord,
    {
        self.by_key.remove_entry(key).map(|(key, alt_key)| {
            let value = self
                .by_alt_key
                .remove(&alt_key)
                .expect("BUG: missing foreign key");
            (key, alt_key, value)
        })
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
