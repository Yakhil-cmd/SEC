### Title
ckETH/ckERC20 Minter Accepts Non-Finalized Block Tag for Deposit Scraping Without Validation - (`rs/ethereum/cketh/minter/src/state.rs`)

---

### Summary

The ckETH minter canister's `validate_config()` function does not enforce that `ethereum_block_height` is set to `Finalized` (or at minimum `Safe`) when operating on Ethereum PoS. The `CandidBlockTag::Latest` variant is the **default** value of the enum and is accepted without rejection. If the minter is configured with `Latest`, it scrapes deposit logs from non-finalized Ethereum blocks and mints ckETH/ckERC20 tokens against them. A reorg that removes those blocks would leave the minted tokens unbacked, causing a conservation violation in the chain-fusion bridge.

---

### Finding Description

The `CandidBlockTag` enum in `rs/ethereum/cketh/minter/src/endpoints.rs` exposes three variants — `Latest`, `Safe`, and `Finalized` — and marks `Latest` as the Rust `Default`: [1](#0-0) 

The `ethereum_block_height` field of `State` is set directly from this enum at init time and can be changed via `UpgradeArg`: [2](#0-1) [3](#0-2) 

The `validate_config()` function, called on every init and upgrade, checks only nonce, ledger ID, and withdrawal amount. It contains **no check** that `ethereum_block_height` is `Finalized` or `Safe`: [4](#0-3) 

The deposit scraping path reads this field directly to determine which Ethereum block tag to query: [5](#0-4) 

When `ethereum_block_height` is `Latest`, `update_last_observed_block_number()` queries the latest (non-finalized) Ethereum block, and `scrape_until_block` then fetches and processes deposit logs up to that block, triggering minting: [6](#0-5) 

The minting is irreversible once the ICRC-1 ledger transfer succeeds: [7](#0-6) 

---

### Impact Explanation

If `ethereum_block_height` is set to `Latest` (the default), the minter mints ckETH or ckERC20 tokens against deposit events from Ethereum blocks that have not been finalized by the PoS finality gadget. An Ethereum reorg that removes those blocks would eliminate the on-chain deposit, but the already-minted IC tokens remain. This breaks the 1:1 backing invariant of the chain-fusion bridge, allowing an attacker to obtain unbacked ckETH/ckERC20 tokens — a direct loss-of-funds scenario for the protocol's reserve.

The `Safe` tag is also weaker than `Finalized` for Ethereum PoS: "safe" blocks are justified but not yet finalized and can still be reorged under a finality-gadget stall attack (the exact scenario described in the external report).

---

### Likelihood Explanation

The production mainnet minter is currently initialized with `Finalized` (confirmed by test fixtures and the testnet README). However:

1. `CandidBlockTag::Latest` is the Rust `Default`, so any deployment that omits the field gets the unsafe value.
2. A governance upgrade proposal passing `ethereum_block_height = Latest` or `Safe` in `UpgradeArg` is accepted without error by `validate_config()`.
3. The `UpgradeArg` field is publicly documented and changeable: [8](#0-7) 

A governance mistake, a malicious proposal, or a future deployment that relies on the Rust default would silently enable the unsafe mode. The code provides no guardrail.

---

### Recommendation

In `validate_config()` (`rs/ethereum/cketh/minter/src/state.rs`), add an explicit check that rejects `CandidBlockTag::Latest` (and optionally warns on `Safe`) for Ethereum Mainnet:

```rust
if self.ethereum_block_height == CandidBlockTag::Latest {
    return Err(InvalidStateError::InvalidEthereumBlockHeight(
        "ethereum_block_height must not be Latest for Ethereum PoS; \
         use Finalized to prevent reorg-based double-minting".to_string(),
    ));
}
```

Additionally, change the `Default` implementation of `CandidBlockTag` from `Latest` to `Finalized` to eliminate the unsafe default.

---

### Proof of Concept

1. Deploy or upgrade the ckETH minter with `ethereum_block_height = Latest` (accepted without error by `validate_config()`).
2. Submit an ETH deposit transaction. The minter's timer fires, calls `update_last_observed_block_number()` with `BlockTag::Latest`, and scrapes logs up to the latest (non-finalized) block.
3. The deposit event is found; `mint()` is called and ckETH is credited to the depositor's IC account.
4. Trigger (or observe) an Ethereum reorg that removes the deposit block. The on-chain ETH deposit no longer exists.
5. The ckETH tokens remain in the depositor's account, unbacked by ETH — the minter's reserve is now undercollateralized.

The root cause is the missing `ethereum_block_height` validation in: [4](#0-3) 

with the unsafe default defined at: [9](#0-8)

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

**File:** rs/ethereum/cketh/minter/src/state.rs (L61-61)
```rust
    pub ethereum_block_height: CandidBlockTag,
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L144-173)
```rust
impl State {
    pub fn validate_config(&self) -> Result<(), InvalidStateError> {
        if self.ecdsa_key_name.trim().is_empty() {
            return Err(InvalidStateError::InvalidEcdsaKeyName(
                "ecdsa_key_name cannot be blank".to_string(),
            ));
        }
        if self.cketh_ledger_id == Principal::anonymous() {
            return Err(InvalidStateError::InvalidLedgerId(
                "ledger_id cannot be the anonymous principal".to_string(),
            ));
        }
        if self.cketh_minimum_withdrawal_amount == Wei::ZERO {
            return Err(InvalidStateError::InvalidMinimumWithdrawalAmount(
                "minimum_withdrawal_amount must be positive".to_string(),
            ));
        }
        let cketh_ledger_transfer_fee = match self.ethereum_network {
            EthereumNetwork::Mainnet => Wei::new(2_000_000_000_000),
            EthereumNetwork::Sepolia => Wei::new(10_000_000_000),
        };
        if self.cketh_minimum_withdrawal_amount < cketh_ledger_transfer_fee {
            return Err(InvalidStateError::InvalidMinimumWithdrawalAmount(
                "minimum_withdrawal_amount must cover ledger transaction fee, \
                otherwise ledger can return a BadBurn error that should be returned to the user"
                    .to_string(),
            ));
        }
        Ok(())
    }
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L528-530)
```rust
        if let Some(block_height) = ethereum_block_height {
            self.ethereum_block_height = block_height;
        }
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L73-128)
```rust
        let block_index = match client
            .transfer(TransferArg {
                from_subaccount: None,
                to: event.beneficiary(),
                fee: None,
                created_at_time: None,
                memo: Some((&event).into()),
                amount: event.value(),
            })
            .await
        {
            Ok(Ok(block_index)) => block_index.0.to_u64().expect("nat does not fit into u64"),
            Ok(Err(err)) => {
                log!(INFO, "Failed to mint {token_symbol}: {event:?} {err}");
                error_count += 1;
                // minting failed, defuse guard
                ScopeGuard::into_inner(prevent_double_minting_guard);
                continue;
            }
            Err(err) => {
                log!(
                    INFO,
                    "Failed to send a message to the ledger ({ledger_canister_id}): {err:?}"
                );
                error_count += 1;
                // minting failed, defuse guard
                ScopeGuard::into_inner(prevent_double_minting_guard);
                continue;
            }
        };
        mutate_state(|s| {
            process_event(
                s,
                match &event {
                    ReceivedEvent::Eth(event) => EventType::MintedCkEth {
                        event_source: event.source(),
                        mint_block_index: LedgerMintIndex::new(block_index),
                    },

                    ReceivedEvent::Erc20(event) => EventType::MintedCkErc20 {
                        event_source: event.source(),
                        mint_block_index: LedgerMintIndex::new(block_index),
                        erc20_contract_address: event.erc20_contract_address,
                        ckerc20_token_symbol: token_symbol.clone(),
                    },
                },
            )
        });
        log!(
            INFO,
            "Minted {} {token_symbol} to {} in block {block_index}",
            event.value(),
            event.beneficiary()
        );
        // minting succeeded, defuse guard
        ScopeGuard::into_inner(prevent_double_minting_guard);
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
