### Title
Unsupported ERC-20 Token Deposits to ckETH Helper Contract Result in Permanently Stuck Funds - (File: `rs/ethereum/cketh/minter/src/eth_logs/scraping.rs`)

### Summary
The ckETH minter's `ReceivedEthOrErc20LogScraping` filters Ethereum log scraping to only supported ERC-20 token addresses, but the `DepositHelperWithSubaccount.sol` helper contract accepts deposits of **any** ERC-20 token without restriction. Any user who deposits an unsupported ERC-20 token via the helper contract will have their tokens permanently transferred to the minter's Ethereum address with no ckERC20 minting on the IC and no recovery mechanism.

### Finding Description
The `ReceivedEthOrErc20LogScraping::next_scrape()` function constructs Ethereum `eth_getLogs` topic filters that include only the zero address (for ETH deposits) and the addresses of currently supported ERC-20 tokens from `state.ckerc20_tokens`:

```rust
topics.push(
    once(Hex32::from([0_u8; 32]))
        .chain(erc20_smart_contracts_addresses_as_topics(state))
        .collect::<Vec<_>>()
        .into(),
);
``` [1](#0-0) 

The helper function `erc20_smart_contracts_addresses_as_topics` only iterates over `state.ckerc20_tokens.alt_keys()`, which is the set of currently registered supported ERC-20 contract addresses: [2](#0-1) 

Meanwhile, the `depositErc20()` function in `DepositHelperWithSubaccount.sol` accepts any ERC-20 address with only a zero-address check:

```solidity
function depositErc20(address erc20Address, uint256 amount, bytes32 principal, bytes32 subaccount) public {
    require(erc20Address != ZERO_ADDRESS, "ERC20: depositErc20 from the zero address");
    IERC20 erc20Token = IERC20(erc20Address);
    erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
    emit ReceivedEthOrErc20(erc20Address, msg.sender, amount, principal, subaccount);
}
``` [3](#0-2) 

When a user deposits an unsupported ERC-20 token:
1. `safeTransferFrom` succeeds — tokens move to the minter's Ethereum address.
2. `ReceivedEthOrErc20` event is emitted with the unsupported token address as the first indexed topic.
3. The minter's `eth_getLogs` call uses a topic filter that does **not** include the unsupported token address.
4. The event is never returned, never parsed, never minted.
5. The tokens are permanently stuck at the minter's Ethereum address.

This is explicitly acknowledged in the project documentation: [4](#0-3) 

> "funds of unsupported ERC-20 tokens could be deposited via the helper smart contract, but the minter will not know anything about it."

Even if the token is later added as a supported ckERC20 token via an NNS upgrade proposal, the historical deposit events are not retroactively re-scraped. The `last_scraped_block_number` for `LogScrapingId::EthOrErc20DepositWithSubaccount` advances continuously and is not rewound when a new token is added: [5](#0-4) 

The minter has no mechanism to send ERC-20 tokens back to users from its Ethereum address.

### Impact Explanation
User ERC-20 tokens are permanently and irrecoverably lost. The minter's Ethereum address accumulates unsupported ERC-20 tokens with no on-chain or off-chain recovery path. This is a **chain-fusion ledger conservation bug**: real user funds are transferred to the minter's custody but never credited on the IC side. The impact is direct financial loss for any user who deposits an ERC-20 token that is not in the minter's `ckerc20_tokens` list.

### Likelihood Explanation
Medium. The helper contract's `depositErc20()` function is publicly callable by any Ethereum user and accepts any ERC-20 address. A user who does not first query `get_minter_info` to verify `supported_ckerc20_tokens` — or who deposits a token that was previously supported but later removed — will silently lose their funds. The Ethereum transaction succeeds with no revert, giving the user no on-chain signal that the deposit was invalid. The documentation warning is easy to miss.

### Recommendation
Enforce the supported token whitelist at the helper smart contract level, analogous to the fix recommended in the external report. The `depositErc20()` function should revert if `erc20Address` is not in a minter-controlled whitelist of supported ERC-20 contracts. This prevents tokens from ever reaching the minter's address if they cannot be processed. Alternatively, implement a minter-controlled recovery endpoint (callable only by governance) that can issue an Ethereum transaction to return stuck ERC-20 tokens to their depositors.

### Proof of Concept
1. User identifies an ERC-20 token (e.g., any token not in `supported_ckerc20_tokens`) and approves the helper contract to spend their tokens.
2. User calls `depositErc20(unsupportedTokenAddress, amount, encodedPrincipal, subaccount)` on `0x18901044688D3756C35Ed2b36D93e6a5B8e00E68`.
3. `safeTransferFrom` succeeds; tokens move to the minter's Ethereum address. `ReceivedEthOrErc20` event is emitted.
4. On the IC side, `scrape_logs()` calls `scrape_until_block::<ReceivedEthOrErc20LogScraping>()`. [6](#0-5) 
5. `next_scrape()` builds a topic filter containing only `[0x000...000, supportedToken1, supportedToken2, ...]` — `unsupportedTokenAddress` is absent. [7](#0-6) 
6. `eth_getLogs` returns no matching events. `register_deposit_events` is called with an empty list. No mint occurs. [8](#0-7) 
7. `last_scraped_block_number` advances past the deposit block. The deposit event is permanently skipped. User's tokens are stuck at the minter's Ethereum address with no recourse.

### Citations

**File:** rs/ethereum/cketh/minter/src/eth_logs/scraping.rs (L96-121)
```rust
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
```

**File:** rs/ethereum/cketh/minter/src/eth_logs/scraping.rs (L124-129)
```rust
fn erc20_smart_contracts_addresses_as_topics(state: &State) -> impl Iterator<Item = Hex32> + '_ {
    state
        .ckerc20_tokens
        .alt_keys()
        .map(|address| Hex32::from(<[u8; 32]>::from(address)))
}
```

**File:** rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol (L511-532)
```text
    function depositErc20(
        address erc20Address,
        uint256 amount,
        bytes32 principal,
        bytes32 subaccount
    ) public {
        require(erc20Address != ZERO_ADDRESS, "ERC20: depositErc20 from the zero address");
        IERC20 erc20Token = IERC20(erc20Address);
        erc20Token.safeTransferFrom(
            msg.sender,
            minterAddress,
            amount
        );

        emit ReceivedEthOrErc20(
            erc20Address,
            msg.sender,
            amount,
            principal,
            subaccount
        );
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

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L156-158)
```rust
    scrape_until_block::<ReceivedEthLogScraping>(last_block_number, max_block_spread).await;
    scrape_until_block::<ReceivedErc20LogScraping>(last_block_number, max_block_spread).await;
    scrape_until_block::<ReceivedEthOrErc20LogScraping>(last_block_number, max_block_spread).await;
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L265-268)
```rust
        match result {
            Ok((events, errors)) => {
                register_deposit_events(S::ID, events, errors);
                mutate_state(|s| S::update_last_scraped_block_number(s, to_block));
```
