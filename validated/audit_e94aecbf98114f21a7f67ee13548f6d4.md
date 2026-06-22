### Title
Unsupported ERC-20 Tokens Deposited via `depositErc20` Are Permanently Irrecoverable at the ckETH Minter Address - (File: rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol, rs/ethereum/cketh/minter/src/eth_logs/scraping.rs)

### Summary
The `CkDeposit` helper smart contract's `depositErc20` function accepts any ERC-20 token address without enforcing a whitelist, transferring tokens directly to the minter's Ethereum address. The ckETH minter's log scraping, however, only fetches and processes `ReceivedErc20` events for tokens in its `ckerc20_tokens` supported list. Any ERC-20 token deposited that is not in the supported list is silently ignored: no ckERC20 is minted, no recovery path exists in the minter canister, and the tokens are permanently locked at the minter's Ethereum address.

### Finding Description
**Deposit path (no whitelist):**

In `DepositHelperWithSubaccount.sol`, the `depositErc20` function performs only a zero-address check and then unconditionally transfers any ERC-20 token to the minter address:

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
``` [1](#0-0) 

**Log scraping path (strict whitelist):**

`ReceivedErc20LogScraping::next_scrape` in `scraping.rs` builds the `eth_getLogs` topic filter using only the addresses of currently supported tokens. Any `ReceivedErc20` event whose `erc20ContractAddress` topic does not match a supported token is never fetched from the RPC providers:

```rust
topics.push(
    erc20_smart_contracts_addresses_as_topics(state)
        .collect::<Vec<_>>()
        .into(),
);
``` [2](#0-1) 

Similarly, `ReceivedEthOrErc20LogScraping::next_scrape` (the subaccount-aware scraper) also restricts its second topic to the zero address (for ETH) plus supported ERC-20 addresses only: [3](#0-2) 

**No recovery mechanism:**

The minter exposes no admin or governance endpoint to rescue ERC-20 tokens held at its Ethereum address for unsupported token contracts. The `withdraw_erc20` endpoint enforces `find_ck_erc20_token_by_ledger_id` and returns `TokenNotSupported` for any unrecognized ledger ID, so the withdrawal path is also blocked: [4](#0-3) 

The asymmetry is structurally identical to the reported Solidity bug: deposit is permissive (no supported-token check), retrieval is restrictive (only supported tokens are processed).

### Impact Explanation
Any ERC-20 tokens sent to the minter's Ethereum address via `depositErc20` for a token not in `ckerc20_tokens` are permanently lost. The minter never observes the deposit event (it is filtered out at the `eth_getLogs` topic level), never mints ckERC20, and has no mechanism to issue a refund or forward the tokens. This is a direct, irreversible ledger conservation violation: real ERC-20 value enters the system but can never exit.

### Likelihood Explanation
The scenario is reachable by any unprivileged Ethereum user (a chain-fusion user) without any privileged access. Realistic triggers include:
- A user deposits a token that was previously supported but has since been removed from the minter's supported list.
- A user mistakenly uses the wrong ERC-20 contract address.
- A user deposits a token that has not yet been added to the supported list, expecting it to be processed retroactively.

The documentation acknowledges this risk with a warning, but the absence of any on-chain or canister-side enforcement means the loss is silent and irreversible.

### Recommendation
Add a supported-token check inside `depositErc20` in the helper smart contract, or alternatively add a governance-controlled rescue function in the minter canister that can issue an Ethereum transaction to transfer stuck ERC-20 tokens back to a designated recovery address. The fix should mirror the recommendation in the original report: enforce the supported-asset check at the point of deposit, not only at the point of retrieval.

### Proof of Concept
1. Query `get_minter_info` on the ckETH minter to obtain the `deposit_with_subaccount_helper_contract_address` and the list of `supported_ckerc20_tokens`.
2. Choose any ERC-20 token whose contract address is **not** in `supported_ckerc20_tokens`.
3. Call `approve(helper_contract, amount)` on that ERC-20 contract.
4. Call `depositErc20(unsupported_erc20_address, amount, principal, subaccount)` on the `CkDeposit` helper contract. The `safeTransferFrom` succeeds and the tokens are transferred to the minter's Ethereum address. A `ReceivedEthOrErc20` event is emitted.
5. Observe that `ReceivedEthOrErc20LogScraping::next_scrape` builds a topic filter that excludes `unsupported_erc20_address`. The minter's `scrape_logs` timer never fetches this event.
6. No ckERC20 is minted. The tokens remain at the minter's Ethereum address indefinitely with no recovery path available through any minter endpoint.

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

**File:** rs/ethereum/cketh/minter/src/eth_logs/scraping.rs (L70-91)
```rust
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

**File:** rs/ethereum/cketh/minter/src/main.rs (L418-428)
```rust
    let ckerc20_token = read_state(|s| s.find_ck_erc20_token_by_ledger_id(&ckerc20_ledger_id))
        .ok_or_else(|| {
            let supported_ckerc20_tokens: BTreeSet<_> = read_state(|s| {
                s.supported_ck_erc20_tokens()
                    .map(|token| token.into())
                    .collect()
            });
            WithdrawErc20Error::TokenNotSupported {
                supported_tokens: Vec::from_iter(supported_ckerc20_tokens),
            }
        })?;
```
