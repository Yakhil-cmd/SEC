### Title
ckETH/ckERC-20 Minter `CandidBlockTag` Defaults to `Latest`, Enabling Reorg-Based Unbacked Token Minting — (File: `rs/ethereum/cketh/minter/src/endpoints.rs`)

---

### Summary

The `CandidBlockTag` enum in the ckETH minter uses `#[default]` on `Latest` — the least safe Ethereum commitment level with respect to chain reorganizations. When the minter is configured with `Latest` or `Safe`, it mints ckETH/ckERC-20 tokens for deposits in non-finalized Ethereum blocks. Because the minter has no mechanism to detect or reverse minting after a reorg, this can produce unbacked ckETH and enable fund theft analogous to the reported reorg-based vault-stealing attack.

---

### Finding Description

**Root cause — dangerous default in `CandidBlockTag`:**

In `rs/ethereum/cketh/minter/src/endpoints.rs`, the `CandidBlockTag` enum places `#[default]` on `Latest`:

```rust
pub enum CandidBlockTag {
    #[default]
    Latest,   // ← Rust default; blocks can be reorganized
    Safe,     // ~2 epochs behind; still reorganizable
    Finalized,// cryptographically finalized
}
``` [1](#0-0) 

This means any code path that calls `CandidBlockTag::default()` — including `InitArg` structs constructed with `..Default::default()` patterns — silently selects `Latest`, the option with the weakest reorg protection.

**Minting pipeline has no finality re-check:**

`scrape_logs()` calls `update_last_observed_block_number()`, which queries Ethereum using the configured `ethereum_block_height`:

```rust
let block_height = read_state(State::ethereum_block_height);
match read_state(rpc_client)
    .get_block_by_number(block_height.clone())
``` [2](#0-1) 

The returned block number is used as the upper bound for `eth_getLogs`. If `ethereum_block_height` is `Latest` or `Safe`, the minter scrapes and processes deposit events from blocks that have not been cryptographically finalized. [3](#0-2) 

**No un-mint mechanism exists:**

Once `register_deposit_events()` records a deposit and `mint()` issues ckETH to the beneficiary, the state transition is permanent. The `EventType` enum has no `RevertedDeposit` variant, and the minter never re-validates previously processed deposits against the current canonical Ethereum chain. [4](#0-3) 

**`UpgradeArg` allows switching to `Latest` at any time:**

The `UpgradeArg` struct exposes `ethereum_block_height: Option<CandidBlockTag>`, allowing governance to change the block tag to `Latest` or `Safe` post-deployment: [5](#0-4) 

**Test confirms `Safe` tag causes minting from non-finalized blocks:**

The test `should_skip_scrapping_when_last_seen_block_newer_than_current_height` explicitly demonstrates that when `ethereum_block_height = Safe`, the minter mints ckETH for deposits in safe (non-finalized) blocks: [6](#0-5) 

---

### Impact Explanation

If the minter is configured with `Latest` or `Safe`, an Ethereum chain reorganization can cause ckETH/ckERC-20 to be minted for deposits that no longer exist on the canonical chain. This:

1. Breaks the 1:1 peg between ckETH and ETH — unbacked ckETH exists on the IC ledger.
2. Allows the attacker to redeem the unbacked ckETH for real ETH from the minter's reserves, effectively stealing ETH from the pool backing honest depositors.
3. Affects all three scraping paths: `ReceivedEthLogScraping`, `ReceivedErc20LogScraping`, and `ReceivedEthOrErc20LogScraping`. [7](#0-6) 

---

### Likelihood Explanation

**Low.** The production mainnet deployment explicitly sets `ethereum_block_height = Finalized` at install time: [8](#0-7) 

However, the `#[default]` on `Latest` elevates risk in two realistic scenarios:

1. **New deployments** of ckETH-like minters (e.g., for new chain-key tokens) that construct `InitArg` using `Default::default()` patterns will silently receive `Latest`.
2. **Governance upgrades** can switch `ethereum_block_height` to `Latest` or `Safe` via `UpgradeArg`, and Ethereum reorgs — while rare on mainnet — have occurred historically (including multi-block reorgs on Ethereum PoS).

---

### Recommendation

1. **Change the `#[default]` on `CandidBlockTag`** from `Latest` to `Finalized`. `Finalized` is the safe default for any production chain-fusion minter.
2. **Add validation in `InitArg::try_from()`** to reject `Latest` as a valid `ethereum_block_height`, or at minimum emit a prominent warning.
3. **Add a guard in the upgrade path** that rejects or warns when `ethereum_block_height` is changed to `Latest` or `Safe`. [9](#0-8) 

---

### Proof of Concept

1. Deploy a new ckETH-like minter using `CandidBlockTag::default()` (resolves to `Latest`) in `InitArg`.
2. Attacker calls the Ethereum helper contract to deposit ETH; the `ReceivedEth` event is emitted in block `N` (latest, not finalized).
3. On the next timer tick, `scrape_logs()` queries `eth_getBlockByNumber("latest")`, receives block `N`, and scrapes logs up to `N`. The deposit event is found and `mint()` issues ckETH to the attacker on the IC ledger.
4. An Ethereum chain reorganization removes block `N` (e.g., a competing fork wins). The deposit transaction no longer exists on the canonical chain; the ETH was never actually transferred to the minter's Ethereum address.
5. The attacker retains the ckETH on the IC. The minter's `minted_events` map permanently records the mint; no reversal is possible.
6. The attacker calls `withdraw_eth` (or equivalent), burning the ckETH and receiving real ETH from the minter's reserves — ETH that was contributed by other honest depositors.

### Citations

**File:** rs/ethereum/cketh/minter/src/endpoints.rs (L121-138)
```rust
#[derive(Clone, Eq, PartialEq, Debug, Default, CandidType, Decode, Deserialize, Encode)]
#[cbor(index_only)]
pub enum CandidBlockTag {
    /// The latest mined block.
    #[default]
    #[cbor(n(0))]
    Latest,
    /// The latest safe head block.
    /// See
    /// <https://www.alchemy.com/overviews/ethereum-commitment-levels#what-are-ethereum-commitment-levels>
    #[cbor(n(1))]
    Safe,
    /// The latest finalized block.
    /// See
    /// <https://www.alchemy.com/overviews/ethereum-commitment-levels#what-are-ethereum-commitment-levels>
    #[cbor(n(2))]
    Finalized,
}
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

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L161-183)
```rust
pub async fn update_last_observed_block_number() -> Option<BlockNumber> {
    let block_height = read_state(State::ethereum_block_height);
    match read_state(rpc_client)
        .get_block_by_number(block_height.clone())
        .with_cycles(MIN_ATTACHED_CYCLES)
        .try_send()
        .await
        .reduce_with_strategy(NoReduction)
    {
        Ok(latest_block) => {
            let block_number = Some(BlockNumber::from(latest_block.number));
            mutate_state(|s| s.last_observed_block_number = block_number);
            block_number
        }
        Err(e) => {
            log!(
                INFO,
                "Failed to get the latest {block_height:?} block number: {e:?}"
            );
            read_state(|s| s.last_observed_block_number)
        }
    }
}
```

**File:** rs/ethereum/cketh/minter/src/state/event.rs (L17-54)
```rust
pub enum EventType {
    /// The minter initialization event.
    /// Must be the first event in the log.
    #[n(0)]
    Init(#[n(0)] InitArg),
    /// The minter upgraded with the specified arguments.
    #[n(1)]
    Upgrade(#[n(0)] UpgradeArg),
    /// The minter discovered a ckETH deposit in the helper contract logs.
    #[n(2)]
    AcceptedDeposit(#[n(0)] ReceivedEthEvent),
    /// The minter discovered an invalid ckETH deposit in the helper contract logs.
    #[n(4)]
    InvalidDeposit {
        /// The unique identifier of the deposit on the Ethereum network.
        #[n(0)]
        event_source: EventSource,
        /// The reason why minter considers the deposit invalid.
        #[n(1)]
        reason: String,
    },
    /// The minter minted ckETH in response to a deposit.
    #[n(5)]
    MintedCkEth {
        /// The unique identifier of the deposit on the Ethereum network.
        #[n(0)]
        event_source: EventSource,
        /// The transaction index on the ckETH ledger.
        #[cbor(n(1), with = "crate::cbor::id")]
        mint_block_index: LedgerMintIndex,
    },
    /// The minter processed the helper smart contract logs up to the specified height.
    #[n(6)]
    SyncedToBlock {
        /// The last processed block number for ETH helper contract (inclusive).
        #[n(0)]
        block_number: BlockNumber,
    },
```

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

**File:** rs/ethereum/cketh/minter/tests/cketh.rs (L976-1001)
```rust
#[test]
fn should_skip_scrapping_when_last_seen_block_newer_than_current_height() {
    let safe_block_number = LAST_SCRAPED_BLOCK_NUMBER_AT_INSTALL + 100;
    let finalized_block_number = safe_block_number - 32;
    let cketh = CkEthSetup::default().check_audit_logs_and_upgrade(UpgradeArg {
        ethereum_block_height: Some(CandidBlockTag::Safe),
        ..Default::default()
    });
    let received_eth_event_topic = cketh.received_eth_event_topic();
    cketh.env.tick();

    let cketh = cketh
        .deposit(DepositParams::default())
        .with_mock_eth_get_block_by_number(move |mock| {
            mock.with_request_params(json!(["safe", false]))
                .respond_for_all_with(block_response(safe_block_number))
        })
        .with_mock_eth_get_logs(move |mock| {
            mock.with_request_params(json!([{
                "fromBlock": BlockNumber::from(LAST_SCRAPED_BLOCK_NUMBER_AT_INSTALL + 1),
                "toBlock": BlockNumber::from(safe_block_number),
                "address": [ETH_HELPER_CONTRACT_ADDRESS],
                "topics": [received_eth_event_topic]
            }]))
        })
        .expect_mint();
```

**File:** rs/ethereum/cketh/minter/src/eth_logs/scraping.rs (L39-92)
```rust
#[derive(Clone, PartialEq, Eq, Debug)]
pub struct Scrape {
    pub contract_address: Address,
    pub last_scraped_block_number: BlockNumber,
    pub topics: Vec<Topic>,
}

pub enum ReceivedEthLogScraping {}

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

pub enum ReceivedErc20LogScraping {}

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

**File:** rs/ethereum/cketh/mainnet/minter_proposal.md (L21-21)
```markdown
didc encode -d cketh_minter.did -t '(MinterArg)' '(variant { InitArg = record { ethereum_network = variant { Mainnet }; ecdsa_key_name = "key_1"; ethereum_contract_address = opt "0x7574eB42cA208A4f6960ECCAfDF186D627dCC175"; ledger_id = principal "ss2fx-dyaaa-aaaar-qacoq-cai"; ethereum_block_height = variant { Finalized }; minimum_withdrawal_amount = 30_000_000_000_000_000; next_transaction_nonce = 0; last_scraped_block_number = 18676637 } })'
```

**File:** rs/ethereum/cketh/minter/src/lifecycle/init.rs (L36-113)
```rust
impl TryFrom<InitArg> for State {
    type Error = InvalidStateError;
    fn try_from(
        InitArg {
            ethereum_network,
            ecdsa_key_name,
            ethereum_contract_address,
            ledger_id,
            ethereum_block_height,
            minimum_withdrawal_amount,
            next_transaction_nonce,
            last_scraped_block_number,
            evm_rpc_id,
        }: InitArg,
    ) -> Result<Self, Self::Error> {
        use std::str::FromStr;

        let initial_nonce = TransactionNonce::try_from(next_transaction_nonce)
            .map_err(|e| InvalidStateError::InvalidTransactionNonce(format!("ERROR: {e}")))?;
        let minimum_withdrawal_amount = Wei::try_from(minimum_withdrawal_amount).map_err(|e| {
            InvalidStateError::InvalidMinimumWithdrawalAmount(format!("ERROR: {e}"))
        })?;
        let eth_helper_contract_address = ethereum_contract_address
            .map(|a| Address::from_str(&a))
            .transpose()
            .map_err(|e| {
                InvalidStateError::InvalidEthereumContractAddress(format!("ERROR: {e}"))
            })?;
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
        let state = Self {
            ethereum_network,
            ecdsa_key_name,
            pending_withdrawal_principals: Default::default(),
            eth_transactions: EthTransactions::new(initial_nonce),
            cketh_ledger_id: ledger_id,
            cketh_minimum_withdrawal_amount: minimum_withdrawal_amount,
            ethereum_block_height,
            first_scraped_block_number,
            last_observed_block_number: None,
            events_to_mint: Default::default(),
            minted_events: Default::default(),
            ecdsa_public_key: None,
            invalid_events: Default::default(),
            eth_balance: Default::default(),
            skipped_blocks: Default::default(),
            active_tasks: Default::default(),
            http_request_counter: 0,
            last_transaction_price_estimate: None,
            ledger_suite_orchestrator_id: None,
            evm_rpc_id,
            ckerc20_tokens: Default::default(),
            erc20_balances: Default::default(),
            log_scrapings,
        };
        state.validate_config()?;
        Ok(state)
    }
```
