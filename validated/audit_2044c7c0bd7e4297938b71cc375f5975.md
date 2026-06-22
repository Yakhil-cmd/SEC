### Title
Unsupported ERC-20 Tokens Deposited via Helper Contracts Are Permanently Locked in the Minter's Ethereum Address - (File: `rs/ethereum/cketh/minter/ERC20DepositHelper.sol`, `rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol`)

---

### Summary

The ckETH minter's two Ethereum-side helper contracts — `CkErc20Deposit` and `CkDeposit` — accept any ERC-20 token address without validating it against the minter's supported token whitelist. When a user deposits an unsupported ERC-20 token, the tokens are transferred to the minter's Ethereum address, but the IC minter canister's log-scraping logic silently ignores the deposit event because it filters `eth_getLogs` calls by supported token addresses only. The tokens become permanently locked in the minter's Ethereum address with no automated recovery path. Recovery requires a governance-approved minter upgrade, creating a centralized dependency identical in structure to the M-01 finding.

---

### Finding Description

**Root cause — helper contracts enforce no token whitelist:**

`CkErc20Deposit.deposit()` in `ERC20DepositHelper.sol` accepts any `erc20_address` parameter:

```solidity
function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
    IERC20 erc20Token = IERC20(erc20_address);
    erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);
    emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
}
``` [1](#0-0) 

`CkDeposit.depositErc20()` in `DepositHelperWithSubaccount.sol` only rejects the zero address, but accepts any other ERC-20 address:

```solidity
function depositErc20(address erc20Address, uint256 amount, bytes32 principal, bytes32 subaccount) public {
    require(erc20Address != ZERO_ADDRESS, "ERC20: depositErc20 from the zero address");
    IERC20 erc20Token = IERC20(erc20Address);
    erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
    emit ReceivedEthOrErc20(erc20Address, msg.sender, amount, principal, subaccount);
}
``` [2](#0-1) 

**Root cause — minter log scraping silently drops unsupported token events:**

The IC minter canister's `ReceivedErc20LogScraping::next_scrape()` builds `eth_getLogs` topic filters that include only the currently-supported ERC-20 contract addresses:

```rust
topics.push(
    erc20_smart_contracts_addresses_as_topics(state)
        .collect::<Vec<_>>()
        .into(),
);
``` [3](#0-2) 

Similarly, `ReceivedEthOrErc20LogScraping::next_scrape()` only includes the zero address (for ETH) and supported ERC-20 addresses in the second topic slot:

```rust
topics.push(
    once(Hex32::from([0_u8; 32]))
        .chain(erc20_smart_contracts_addresses_as_topics(state))
        .collect::<Vec<_>>()
        .into(),
);
``` [4](#0-3) 

Because Ethereum's `eth_getLogs` topic filtering is applied server-side, any `ReceivedErc20` or `ReceivedEthOrErc20` event emitted for an unsupported token address is never returned to the minter. The minter never calls `register_deposit_events` for it, never emits `AcceptedErc20Deposit`, and never mints ckERC-20. [5](#0-4) 

The project's own documentation explicitly acknowledges this design gap:

> "Note that the helper smart contract does not enforce any whitelist of allowed ERC-20 tokens. This is enforced by the minter, which fetches logs only for the supported ERC-20 tokens. Therefore, funds of unsupported ERC-20 tokens could be deposited via the helper smart contract, but the minter will not know anything about it." [6](#0-5) 

---

### Impact Explanation

Any ERC-20 tokens deposited via either helper contract for an unsupported token address are transferred to the minter's Ethereum address and permanently locked there. The minter canister has no `removeFunds`-style endpoint or any automated recovery path. Recovery requires a governance proposal to upgrade the minter canister with a new recovery function — a centralized, manual intervention path. Users who deposit unsupported tokens suffer a permanent, unrecoverable loss of funds.

---

### Likelihood Explanation

The likelihood is **medium**. The scenario is reachable by any Ethereum user without any privileged access. Realistic triggers include:

1. A user deposits a token that was previously supported but was later removed from the minter's whitelist (their deposit window closes silently).
2. A user misreads the supported token list or uses a token with a similar name/symbol to a supported one.
3. A user deposits before a newly-requested token is officially added to the whitelist.

The documentation warning exists but is easy to overlook, especially for users interacting programmatically or via third-party frontends that do not surface the warning.

---

### Recommendation

Enforce the supported-token whitelist on-chain in both helper contracts. The `deposit()` function in `CkErc20Deposit` and `depositErc20()` in `CkDeposit` should maintain an owner-managed whitelist of approved ERC-20 contract addresses and revert if the provided `erc20_address` is not on the list. This mirrors the fix described in M-01 and eliminates the silent fund-locking scenario entirely.

---

### Proof of Concept

**Attack path using `CkErc20Deposit`:**

1. User identifies an ERC-20 token (e.g., DAI at `0x6b175474e89094c44da98b954eedeac495271d0f`) that is **not** in the minter's `ckerc20_tokens` map.
2. User calls `CkErc20Deposit.deposit(0x6b175474e89094c44da98b954eedeac495271d0f, amount, principal)` on Ethereum.
3. `safeTransferFrom` succeeds — DAI tokens are transferred to `cketh_minter_main_address`.
4. `ReceivedErc20(0x6b175474..., user, amount, principal)` event is emitted on Ethereum.
5. On the IC side, `ReceivedErc20LogScraping::next_scrape()` builds the `eth_getLogs` filter with topics `[RECEIVED_ERC20_EVENT_TOPIC, [supported_addr_1, supported_addr_2, ...]]`. DAI's address is absent from the second topic list.
6. The `eth_getLogs` RPC call returns no matching logs for this deposit.
7. `register_deposit_events` is never called for this event; no `AcceptedErc20Deposit` event is recorded; no ckERC-20 is minted.
8. DAI tokens remain permanently locked in the minter's Ethereum address. [7](#0-6) [8](#0-7) 

**Attack path using `CkDeposit` (subaccount helper):**

Identical flow using `CkDeposit.depositErc20(unsupported_token, amount, principal, subaccount)`. The `ReceivedEthOrErc20LogScraping` second-topic filter includes only `[0x000...000, supported_addr_1, ...]`; the unsupported token address is absent, so the event is never scraped. [9](#0-8) [2](#0-1)

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

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L235-268)
```rust
async fn scrape_block_range<S>(
    rpc_client: &EvmRpcClient<IcRuntime, CandidResponseConverter, DoubleCycles>,
    contract_address: Address,
    topics: Vec<Topic>,
    block_range: BlockRangeInclusive,
) -> Result<(), MultiCallError<Vec<LogEntry>>>
where
    S: LogScraping,
{
    let mut subranges = VecDeque::new();
    subranges.push_back(block_range);

    while !subranges.is_empty() {
        let range = subranges.pop_front().unwrap();
        let (from_block, to_block) = range.clone().into_inner();

        let result = rpc_client
            .get_logs(vec![contract_address.into_bytes()])
            .with_from_block(from_block)
            .with_to_block(to_block)
            .with_topics(into_evm_topic(topics.clone()))
            .with_cycles(MIN_ATTACHED_CYCLES)
            .with_response_size_estimate(
                ETH_GET_LOGS_INITIAL_RESPONSE_SIZE_ESTIMATE + HEADER_SIZE_LIMIT,
            )
            .try_send()
            .await
            .reduce_with_strategy(NoReduction)
            .map(<S::Parser>::parse_all_logs);

        match result {
            Ok((events, errors)) => {
                register_deposit_events(S::ID, events, errors);
                mutate_state(|s| S::update_last_scraped_block_number(s, to_block));
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L311-344)
```rust
pub fn register_deposit_events(
    scraping_id: LogScrapingId,
    transaction_events: Vec<ReceivedEvent>,
    errors: Vec<ReceivedEventError>,
) {
    for event in transaction_events {
        log!(
            INFO,
            "Received event {event:?}; will mint {} {scraping_id} to {}",
            event.value(),
            event.beneficiary()
        );
        if crate::blocklist::is_blocked(&event.from_address()) {
            log!(
                INFO,
                "Received event from a blocked address: {} for {} {scraping_id}",
                event.from_address(),
                event.value(),
            );
            mutate_state(|s| {
                process_event(
                    s,
                    EventType::InvalidDeposit {
                        event_source: event.source(),
                        reason: format!("blocked address {}", event.from_address()),
                    },
                )
            });
        } else {
            mutate_state(|s| process_event(s, event.into_deposit()));
        }
    }
    if read_state(State::has_events_to_mint) {
        ic_cdk_timers::set_timer(Duration::from_secs(0), async { mint().await });
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
