### Title
Unsupported ERC-20 Tokens Deposited via Helper Contracts Are Permanently Locked with No Recovery Path - (`rs/ethereum/cketh/minter/ERC20DepositHelper.sol`, `rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol`)

---

### Summary

Both ckETH helper smart contracts (`CkErc20Deposit` and `CkDeposit`) accept any ERC-20 token address in their deposit functions without checking against the minter's supported token list. The minter enforces the allowlist only at the log-scraping layer via `eth_getLogs` topic filters. Tokens deposited for unsupported ERC-20 contracts are transferred to the minter's Ethereum address, but the minter never observes the event and never mints ckERC20. Unlike the Gravity.sol analog, there is no admin recovery function — the funds are permanently locked.

---

### Finding Description

**`CkErc20Deposit.deposit()`** in `ERC20DepositHelper.sol`:

```solidity
function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
    IERC20 erc20Token = IERC20(erc20_address);
    erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);
    emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
}
``` [1](#0-0) 

**`CkDeposit.depositErc20()`** in `DepositHelperWithSubaccount.sol`:

```solidity
function depositErc20(address erc20Address, uint256 amount, bytes32 principal, bytes32 subaccount) public {
    require(erc20Address != ZERO_ADDRESS, "ERC20: depositErc20 from the zero address");
    IERC20 erc20Token = IERC20(erc20Address);
    erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
    emit ReceivedEthOrErc20(erc20Address, msg.sender, amount, principal, subaccount);
}
``` [2](#0-1) 

Neither function validates `erc20_address` / `erc20Address` against the minter's supported token list. The only guard is a zero-address check in the newer contract.

The minter enforces the allowlist exclusively at the log-scraping layer. `ReceivedErc20LogScraping::next_scrape()` adds supported ERC-20 contract addresses as Ethereum topic filters:

```rust
topics.push(
    erc20_smart_contracts_addresses_as_topics(state)
        .collect::<Vec<_>>()
        .into(),
);
``` [3](#0-2) 

Similarly, `ReceivedEthOrErc20LogScraping::next_scrape()` filters by zero address (for ETH) and supported ERC-20 addresses:

```rust
topics.push(
    once(Hex32::from([0_u8; 32]))
        .chain(erc20_smart_contracts_addresses_as_topics(state))
        .collect::<Vec<_>>()
        .into(),
);
``` [4](#0-3) 

Because the `eth_getLogs` call filters by supported token addresses, any `ReceivedErc20` or `ReceivedEthOrErc20` event emitted for an unsupported token is **never fetched** by the minter. The minter's own documentation acknowledges this explicitly:

> "Note that the helper smart contract does not enforce any whitelist of allowed ERC-20 tokens. This is enforced by the minter, which fetches logs only for the supported ERC-20 tokens. Therefore, funds of unsupported ERC-20 tokens could be deposited via the helper smart contract, but the minter will not know anything about it." [5](#0-4) 

The minter's Ethereum address is controlled exclusively by the threshold ECDSA key. The only programmatic path to move ERC-20 tokens out of the minter's address is the `withdraw_erc20` flow, which requires burning ckERC20 tokens on the IC ledger. Since no ckERC20 is ever minted for the unsupported deposit, no withdrawal can be initiated. There is no admin-callable recovery function analogous to Gravity.sol's `withdrawERC20`. The deposited tokens are permanently locked.

---

### Impact Explanation

Any user who calls `deposit()` or `depositErc20()` with an ERC-20 token address not in the minter's `supported_ckerc20_tokens` list loses their tokens permanently. The tokens are transferred to the minter's Ethereum address on-chain, but the minter canister never observes the event and never mints the corresponding ckERC20. Because the minter has no recovery endpoint, the funds cannot be returned. This is a direct, irreversible loss of user funds with no admin mitigation path — strictly worse than the Gravity.sol analog where an admin could at least call `withdrawERC20`.

---

### Likelihood Explanation

The entry path requires only a standard Ethereum transaction from any unprivileged user. The helper contracts are publicly callable. Users may mistakenly deposit a token that was recently removed from the supported list, or one they believe is supported but is not yet added. The minter's supported token list changes over time via NNS proposals, creating windows where a previously supported token is no longer scraped. The documentation warning is easy to miss, especially for users interacting via Etherscan's write-contract UI. Likelihood is medium-high given the permissionless nature of the deposit functions and the real-world history of similar user errors in bridge protocols.

---

### Recommendation

Add an on-chain allowlist check in both helper contracts. The supported ERC-20 contract addresses should be stored in the helper contract (updatable by the minter or via a governance-controlled setter) and validated before accepting a deposit:

```solidity
require(supportedTokens[erc20Address], "ERC20: token not supported");
```

Alternatively, the minter canister should expose a query endpoint that the helper contract can call (via a callback pattern or by storing the list on-chain), so that unsupported deposits revert at the Ethereum level rather than silently locking funds. At minimum, the minter should be upgraded to include a privileged recovery function that can return locked unsupported ERC-20 tokens to their depositors.

---

### Proof of Concept

1. User calls `depositErc20(0xUNSUPPORTED_TOKEN, 1000e18, principal, subaccount)` on the `CkDeposit` helper contract (`DepositHelperWithSubaccount.sol`).
2. `safeTransferFrom` succeeds — 1000 tokens are transferred from the user to the minter's Ethereum address.
3. `ReceivedEthOrErc20(0xUNSUPPORTED_TOKEN, user, 1000e18, principal, subaccount)` is emitted.
4. The minter's timer fires and calls `eth_getLogs` with topic filter `[RECEIVED_ETH_OR_ERC20_TOPIC, [0x0, 0xSUPPORTED_TOKEN_1, 0xSUPPORTED_TOKEN_2, ...]]`. Since `0xUNSUPPORTED_TOKEN` is not in the topic list, the event is never returned.
5. No `AcceptedErc20Deposit` event is recorded in the minter's audit log. No ckERC20 is minted.
6. The user's 1000 tokens remain in the minter's Ethereum address indefinitely. No IC-side function exists to recover them. [2](#0-1) [6](#0-5) [7](#0-6)

### Citations

**File:** rs/ethereum/cketh/minter/ERC20DepositHelper.sol (L498-503)
```text
    function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
        IERC20 erc20Token = IERC20(erc20_address);
        erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);

        emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
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

**File:** rs/ethereum/cketh/minter/src/eth_logs/scraping.rs (L77-84)
```rust
        let mut topics: Vec<_> = vec![Topic::Single(Hex32::from(RECEIVED_ERC20_EVENT_TOPIC))];
        // We add token contract addresses as additional topics to match.
        // It has a disjunction semantics, so it will match if event matches any one of these addresses.
        topics.push(
            erc20_smart_contracts_addresses_as_topics(state)
                .collect::<Vec<_>>()
                .into(),
        );
```

**File:** rs/ethereum/cketh/minter/src/eth_logs/scraping.rs (L100-121)
```rust
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

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L53-67)
```rust
        let (token_symbol, ledger_canister_id) = match &event {
            ReceivedEvent::Eth(_) => ("ckETH".to_string(), eth_ledger_canister_id),
            ReceivedEvent::Erc20(event) => {
                if let Some(result) = read_state(|s| {
                    s.ckerc20_tokens
                        .get_entry_alt(&event.erc20_contract_address)
                        .map(|(principal, symbol)| (symbol.to_string(), *principal))
                }) {
                    result
                } else {
                    panic!(
                        "Failed to mint ckERC20: {event:?} Unsupported ERC20 contract address. (This should have already been filtered out by process_event)"
                    )
                }
            }
```
