### Title
Unsupported ERC-20 Token Deposits to `DepositHelperWithSubaccount.sol` Cause Permanent Fund Loss — (`rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol`)

---

### Summary

The `depositErc20` function in the IC chain-fusion helper smart contract does not validate that the deposited ERC-20 token is supported by the ckETH minter. Any user can call `depositErc20` with an arbitrary ERC-20 contract address, causing the ERC-20 tokens to be transferred to the minter's Ethereum address. The minter's log scraping silently ignores events for unsupported tokens, so no ckERC20 is ever minted. The deposited ERC-20 tokens are permanently locked in the minter's Ethereum address with no recovery path.

---

### Finding Description

The `depositErc20` function in `DepositHelperWithSubaccount.sol` performs only a zero-address check on `erc20Address` before executing the `safeTransferFrom`:

```solidity
function depositErc20(
    address erc20Address,
    uint256 amount,
    bytes32 principal,
    bytes32 subaccount
) public {
    require(erc20Address != ZERO_ADDRESS, "ERC20: depositErc20 from the zero address");
    IERC20 erc20Token = IERC20(erc20Address);
    erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
    emit ReceivedEthOrErc20(erc20Address, msg.sender, amount, principal, subaccount);
}
```

There is no check that `erc20Address` corresponds to a token supported by the minter. [1](#0-0) 

On the IC side, `ReceivedEthOrErc20LogScraping::next_scrape` constructs the `eth_getLogs` topic filter to include only the zero address (ETH) and the currently supported ERC-20 contract addresses:

```rust
topics.push(
    once(Hex32::from([0_u8; 32]))
        .chain(erc20_smart_contracts_addresses_as_topics(state))
        .collect::<Vec<_>>()
        .into(),
);
``` [2](#0-1) 

Events for unsupported ERC-20 addresses are therefore never fetched and never processed. The minter's `mint()` function would panic if such an event somehow reached `record_event_to_mint`, but the topic filter prevents that — the tokens are simply silently abandoned. [3](#0-2) 

The official documentation explicitly acknowledges this gap:

> "Note that the helper smart contract does not enforce any whitelist of allowed ERC-20 tokens. This is enforced by the minter, which fetches logs only for the supported ERC-20 tokens. Therefore, funds of unsupported ERC-20 tokens could be deposited via the helper smart contract, but the minter will not know anything about it." [4](#0-3) 

---

### Impact Explanation

Any ERC-20 tokens deposited via `depositErc20` for an unsupported token address are transferred to the minter's Ethereum address (controlled by the threshold ECDSA key) and permanently locked. There is no on-chain recovery function in the helper contract, and the minter has no built-in mechanism to return or sweep unsupported tokens. The user loses their ERC-20 tokens entirely. This is a direct fund-loss outcome for any user who deposits an unsupported token, whether by mistake or because a previously supported token was later removed from the minter's supported list.

---

### Likelihood Explanation

The entry path is fully unprivileged: any Ethereum user can call `depositErc20` with any ERC-20 address. The risk is elevated because:
1. The helper contract address is publicly advertised and callable by anyone.
2. The supported token list changes over time via NNS proposals; a token that was supported at deposit time may be removed later, retroactively making past deposits unrecoverable.
3. Users who misread or copy the wrong ERC-20 contract address will silently lose funds with no on-chain error.

---

### Recommendation

Add a whitelist of supported ERC-20 contract addresses to the `CkDeposit` helper contract, controlled by the minter address (which is set at construction time). The `depositErc20` function should revert if `erc20Address` is not in the whitelist. The minter upgrade process (triggered by NNS proposals via `add_ckerc20_token`) should also call a setter on the helper contract to add the new token address to the whitelist. This mirrors the recommendation in the SKALE report: enforce the constraint at the source (the contract that accepts the transfer) rather than relying solely on the downstream consumer to silently ignore invalid inputs. [5](#0-4) 

---

### Proof of Concept

1. Alice holds 1000 units of `TOKEN_X`, an ERC-20 token not in the minter's `ckerc20_tokens` map.
2. Alice calls `TOKEN_X.approve(helperContract, 1000)` on Ethereum.
3. Alice calls `helperContract.depositErc20(TOKEN_X_ADDRESS, 1000, alicePrincipal, subaccount)`.
4. The helper contract calls `TOKEN_X.safeTransferFrom(alice, minterAddress, 1000)` — this succeeds. Alice's 1000 `TOKEN_X` are now held by the minter's Ethereum address.
5. The helper contract emits `ReceivedEthOrErc20(TOKEN_X_ADDRESS, alice, 1000, alicePrincipal, subaccount)`.
6. The ckETH minter's timer fires and calls `scrape_logs()`. `ReceivedEthOrErc20LogScraping::next_scrape` builds a topic filter that does NOT include `TOKEN_X_ADDRESS`. [6](#0-5) 
7. The `eth_getLogs` RPC call returns no matching logs. The minter never sees Alice's deposit event.
8. No ckERC20 is minted for Alice. Alice's 1000 `TOKEN_X` remain permanently locked in the minter's Ethereum address. There is no recovery mechanism. [7](#0-6)

### Citations

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

**File:** rs/ethereum/cketh/minter/src/state.rs (L199-205)
```rust
        if let ReceivedEvent::Erc20(event) = event {
            assert!(
                self.ckerc20_tokens
                    .contains_alt(&event.erc20_contract_address),
                "BUG: unsupported ERC-20 contract address in event {event:?}"
            )
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

**File:** rs/ethereum/cketh/minter/src/main.rs (L562-573)
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
