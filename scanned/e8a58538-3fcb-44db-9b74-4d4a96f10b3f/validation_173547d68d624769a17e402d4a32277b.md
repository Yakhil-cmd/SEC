### Title
Unsupported ERC-20 Token Deposit Permanently Locks User Funds in ckETH Minter — (`rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol`, `rs/ethereum/cketh/minter/ERC20DepositHelper.sol`)

---

### Summary

Both ckERC20 helper smart contracts (`CkDeposit.depositErc20` and `CkErc20Deposit.deposit`) accept any ERC-20 token address without validating that the token is supported by the IC minter. The minter's log-scraping pipeline, however, filters events by a whitelist of supported ERC-20 contract addresses. Any ERC-20 token deposited via the helper contract that is not on the minter's supported list is silently transferred to the minter's Ethereum address and permanently locked — no ckERC20 is ever minted, and no recovery path exists in the minter's interface.

---

### Finding Description

**Deposit path — no validation:**

`CkDeposit.depositErc20()` in `DepositHelperWithSubaccount.sol`:

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

The only guard is a zero-address check. Any arbitrary ERC-20 token is accepted and transferred to the minter's Ethereum address. [1](#0-0) 

The older `CkErc20Deposit.deposit()` in `ERC20DepositHelper.sol` has the same pattern with no guard at all: [2](#0-1) 

**Withdrawal path — strict whitelist filtering:**

The IC minter's `ReceivedErc20LogScraping::next_scrape()` builds an `eth_getLogs` filter that includes only the supported ERC-20 contract addresses as topics. Events for any other token address are never fetched:

```rust
topics.push(
    erc20_smart_contracts_addresses_as_topics(state)
        .collect::<Vec<_>>()
        .into(),
);
``` [3](#0-2) 

The same filtering applies in `ReceivedEthOrErc20LogScraping::next_scrape()` for the newer helper contract: [4](#0-3) 

Because the minter never observes the deposit event, `register_deposit_events` is never called, no `AcceptedErc20Deposit` state transition occurs, and no ckERC20 is ever minted. The ERC-20 tokens sit in the minter's Ethereum address indefinitely. [5](#0-4) 

**No recovery mechanism:**

The minter's Candid interface exposes no admin endpoint to rescue or return unsupported ERC-20 tokens held at the minter's Ethereum address. The minter's threshold-ECDSA key controls that address, and there is no `rescue_erc20` or equivalent call. [6](#0-5) 

**The project's own documentation acknowledges the gap:**

> "Note that the helper smart contract does not enforce any whitelist of allowed ERC-20 tokens. This is enforced by the minter, which fetches logs only for the supported ERC-20 tokens. Therefore, funds of unsupported ERC-20 tokens could be deposited via the helper smart contract, but the minter will not know anything about it." [7](#0-6) 

---

### Impact Explanation

Any ERC-20 token deposited via either helper contract for an address not in the minter's `ckerc20_tokens` map is permanently locked in the minter's Ethereum address. The minter holds the private key via threshold ECDSA, but exposes no mechanism to sign a recovery transaction for unsupported tokens. The loss is irreversible without an NNS governance upgrade specifically crafted to rescue the funds — a high-friction, non-guaranteed path. This is a direct, permanent loss of user funds.

---

### Likelihood Explanation

The scenario is realistic under several conditions:

1. A user deposits an ERC-20 token that was supported at the time they set up their `approve()` allowance, but the token was subsequently removed from the minter's supported list before the deposit transaction was submitted.
2. A user mistakenly uses the wrong ERC-20 contract address (e.g., a wrapped or bridged variant of a supported token).
3. A user deposits a token in anticipation of it being added to the supported list, before the NNS proposal is executed.

The helper contracts are publicly callable by any Ethereum address with no access control. The documentation warning is the only mitigation, and user error is a well-established real-world risk in DeFi deposit flows.

---

### Recommendation

Add a supported-token whitelist check inside `depositErc20()` in both helper contracts. Since the minter's supported token list is managed on the IC side and not directly accessible from Solidity, the recommended approach is to maintain an on-chain registry of supported ERC-20 addresses in the helper contract itself, updated via the minter's Ethereum address (the contract owner). The `depositErc20` function should revert if `erc20Address` is not in the registry:

```solidity
mapping(address => bool) public supportedTokens;

function depositErc20(
    address erc20Address,
    uint256 amount,
    bytes32 principal,
    bytes32 subaccount
) public {
    require(erc20Address != ZERO_ADDRESS, "ERC20: depositErc20 from the zero address");
    require(supportedTokens[erc20Address], "ERC20: token not supported");
    IERC20 erc20Token = IERC20(erc20Address);
    erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
    emit ReceivedEthOrErc20(erc20Address, msg.sender, amount, principal, subaccount);
}
```

Alternatively, add a rescue function callable only by the minter's Ethereum address to transfer any ERC-20 token balance back to a specified address, providing a recovery path for already-locked funds.

---

### Proof of Concept

1. User calls `IERC20(unsupportedToken).approve(helperContract, amount)` on Ethereum.
2. User calls `CkDeposit.depositErc20(unsupportedToken, amount, principal, subaccount)`.
3. `safeTransferFrom` succeeds — `amount` of `unsupportedToken` is now held at `minterAddress`.
4. `ReceivedEthOrErc20` event is emitted on-chain.
5. The IC minter's `ReceivedEthOrErc20LogScraping::next_scrape()` builds a topic filter containing only supported ERC-20 addresses. The event for `unsupportedToken` does not match any topic and is never returned by `eth_getLogs`.
6. No `AcceptedErc20Deposit` event is recorded in the minter's audit log.
7. No ckERC20 is minted to the user's IC principal.
8. The user's ERC-20 tokens are permanently locked. The minter's Candid interface has no endpoint to recover them.

This is confirmed by the integration test `should_fail_to_mint_from_unsupported_erc20_contract_address`, which demonstrates that the minter emits no `AcceptedErc20Deposit` event and mints nothing when an unsupported ERC-20 address is used — but this test does not model the on-chain token transfer that already occurred. [8](#0-7)

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

**File:** rs/ethereum/cketh/minter/ERC20DepositHelper.sol (L498-503)
```text
    function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
        IERC20 erc20Token = IERC20(erc20_address);
        erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);

        emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
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

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L311-345)
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
    }
```

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L696-750)
```text
service : (MinterArg) -> {
    // Retrieve the Ethereum address controlled by the minter:
    // * Deposits will be transferred from the helper smart contract to this address
    // * Withdrawals will originate from this address
    // IMPORTANT: Do NOT send ETH to this address directly. Use the helper smart contract instead so that the minter
    // knows to which IC principal the funds should be deposited.
    minter_address : () -> (text);

    // Address of the helper smart contract.
    // Returns "N/A" if the helper smart contract is not set.
    // IMPORTANT:
    // * Use this address to send ETH to the minter to convert it to ckETH.
    // * In case the smart contract needs to be updated the returned address will change!
    //   Always check the address before making a transfer.
    smart_contract_address : () -> (text) query;

    // Estimate the price of a transaction issued by the minter when converting ckETH to ETH.
    eip_1559_transaction_price : (opt Eip1559TransactionPriceArg) -> (Eip1559TransactionPrice) query;

    // Returns internal minter parameters
    get_minter_info : () -> (MinterInfo) query;

    // Withdraw the specified amount in Wei to the given Ethereum address.
    // IMPORTANT: The current gas limit is set to 21,000 for a transaction so withdrawals to smart contract addresses will likely fail.
    withdraw_eth : (WithdrawalArg) -> (variant { Ok : RetrieveEthRequest; Err : WithdrawalError });

    // Withdraw the specified amount of ERC-20 tokens to the given Ethereum address.
    withdraw_erc20 : (WithdrawErc20Arg) -> (variant { Ok : RetrieveErc20Request; Err : WithdrawErc20Error });

    // Retrieve the status of a Eth withdrawal request.
    retrieve_eth_status : (nat64) -> (RetrieveEthStatus);

    // Return details of all withdrawals matching the given search parameter.
    withdrawal_status : (WithdrawalSearchParameter) -> (vec WithdrawalDetail) query;

    // Check if an address is blocked by the minter.
    is_address_blocked : (text) -> (bool) query;

    // Retrieve the status of the minter canister.
    //
    // This is a debug endpoint where backwards-compatibility is not guaranteed.
    get_canister_status : () -> (CanisterStatusResponse);

    // Retrieve events from the minter's audit log.
    // The endpoint can return fewer events than requested to bound the response size.
    // IMPORTANT: this endpoint is meant as a debugging tool and is not guaranteed to be backwards-compatible.
    get_events : (record { start : nat64; length : nat64 }) -> (record { events : vec Event; total_event_count : nat64 }) query;

    // Add a ckERC-20 token to be supported by the minter.
    // This call is restricted to the orchestrator ID.
    add_ckerc20_token : (AddCkErc20Token) -> ();

    // Decode ledger memos produced by the minter when minting (deposits) or burning (withdrawals).
    decode_ledger_memo : (DecodeLedgerMemoArgs) -> (DecodeLedgerMemoResult) query;
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

**File:** rs/ethereum/cketh/minter/tests/ckerc20.rs (L1613-1639)
```rust
#[test]
fn should_fail_to_mint_from_unsupported_erc20_contract_address() {
    let ckerc20 = CkErc20Setup::default().add_supported_erc20_tokens();
    let ckusdc = ckerc20.find_ckerc20_token("ckUSDC");
    let unsupported_erc20_address: Address = "0x6b175474e89094c44da98b954eedeac495271d0f"
        .parse()
        .unwrap();
    assert!(
        !ckerc20
            .supported_erc20_contract_addresses()
            .contains(&unsupported_erc20_address)
    );

    ckerc20
        .deposit(DepositCkErc20Params::new(
            ONE_USDC,
            CkErc20Token {
                erc20_contract_address: unsupported_erc20_address.to_string(),
                ..ckusdc.clone()
            },
        ))
        .expect_no_mint()
        .check_events()
        .assert_has_no_event_satisfying(|event| {
            matches!(event, EventPayload::AcceptedErc20Deposit { .. })
        });
}
```
