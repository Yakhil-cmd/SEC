The code is clear enough to make a definitive determination. Let me trace the full path.

**The helper contracts emit `msg.sender` as `from_address`:**

- `EthDepositHelper.sol` line 33: `emit ReceivedEth(msg.sender, msg.value, _principal);`
- `ERC20DepositHelper.sol` line 502: `emit ReceivedErc20(erc20_address, msg.sender, amount, principal);`
- `DepositHelperWithSubaccount.sol` line 504: `emit ReceivedEthOrErc20(ZERO_ADDRESS, msg.sender, msg.value, principal, subaccount);`

**The minter parses `from_address` from the event topic and checks it:**

`parser.rs` line 45: `let from_address = parse_address(&entry.topics[1], event_source)?;`
`deposit.rs` line 323: `if crate::blocklist::is_blocked(&event.from_address())`

If a proxy contract calls the helper, `msg.sender` = proxy address → `from_address` = proxy address → `is_blocked(proxy_address)` = false → minting proceeds.

---

### Title
Blocklist Bypass via Proxy Contract — (`rs/ethereum/cketh/minter/src/blocklist.rs`, `rs/ethereum/cketh/minter/src/deposit.rs`)

### Summary
The ckETH minter's blocklist enforcement checks only the immediate `msg.sender` of the helper contract call (as recorded in the emitted event's `from_address` topic). A blocked EOA can deploy an unblocked proxy smart contract that forwards ETH/ERC-20 to the helper contract, causing the minter to see the proxy's address as `from_address`, bypassing the blocklist entirely.

### Finding Description
All three helper contracts record `msg.sender` — the immediate caller — as the `from_address` in their emitted events: [1](#0-0) [2](#0-1) [3](#0-2) 

The minter's log parser reads `from_address` directly from `entry.topics[1]` (or `topics[2]` for ERC-20): [4](#0-3) [5](#0-4) 

`register_deposit_events` then checks only this field against the blocklist: [6](#0-5) 

`is_blocked` performs a binary search on the static `ETH_ADDRESS_BLOCKLIST`: [7](#0-6) 

Because the proxy contract's address is not on the blocklist, `is_blocked` returns `false` and the deposit proceeds to minting.

There is also a discrepancy with the documented intent: the `ckerc20.adoc` documentation states the minter checks "the sender of the transaction" — in Ethereum terminology this implies `tx.origin` (the signing EOA), not `msg.sender` (the immediate caller). [8](#0-7) 

### Impact Explanation
A blocked Ethereum address (e.g., a sanctioned entity) can receive ckETH or ckERC-20 tokens by routing funds through an unblocked proxy contract. The sanctioned address retains beneficial ownership of the minted tokens on the IC. This directly violates the stated invariant: "ETH is not accepted from nor sent to addresses on this list." [9](#0-8) 

### Likelihood Explanation
The attack requires only deploying a standard Solidity proxy contract (trivial, costs only gas) and calling the helper contract through it. No privileged access, no key compromise, no governance majority is needed. The blocked address controls the proxy and specifies its own IC principal as the recipient. The path is fully concrete and locally testable.

### Recommendation
The helper contracts should be updated to record `tx.origin` instead of `msg.sender` as the `from_address` in emitted events, OR the helper contracts should reject calls from contract addresses (checking `msg.sender == tx.origin`). The latter is simpler and avoids the known pitfalls of `tx.origin` in authorization contexts — it simply prevents contract-to-contract forwarding into the helper, which is consistent with the intended direct-user deposit flow.

### Proof of Concept
State-machine test:
1. Take `SAMPLE_BLOCKED_ADDRESS` from the blocklist.
2. Simulate a `ReceivedEth` log entry where `topics[1]` (the `from_address`) is set to a fresh, non-blocked proxy address (not in `ETH_ADDRESS_BLOCKLIST`), but the beneficial owner is the blocked address.
3. Call `register_deposit_events` with this event.
4. Assert `is_blocked(proxy_address)` returns `false`.
5. Assert that a `MintedCkEth` event is emitted — confirming ckETH is minted despite the blocked address being the ultimate source.

This mirrors the existing test structure in `cketh.rs` `should_block_deposit_from_blocked_address`, but with `from_address` set to the proxy rather than the blocked address directly. [10](#0-9)

### Citations

**File:** rs/ethereum/cketh/minter/EthDepositHelper.sol (L32-34)
```text
    function deposit(bytes32 _principal) public payable {
        emit ReceivedEth(msg.sender, msg.value, _principal);
        cketh_minter_main_address.transfer(msg.value);
```

**File:** rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol (L503-505)
```text
    function depositEth(bytes32 principal, bytes32 subaccount) public payable {
        emit ReceivedEthOrErc20(ZERO_ADDRESS, msg.sender, msg.value, principal, subaccount);
        minterAddress.transfer(msg.value);
```

**File:** rs/ethereum/cketh/minter/ERC20DepositHelper.sol (L498-502)
```text
    function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
        IERC20 erc20Token = IERC20(erc20_address);
        erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);

        emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
```

**File:** rs/ethereum/cketh/minter/src/eth_logs/parser.rs (L45-46)
```rust
        let from_address = parse_address(&entry.topics[1], event_source)?;
        let principal = parse_principal(&entry.topics[2], event_source)?;
```

**File:** rs/ethereum/cketh/minter/src/eth_logs/parser.rs (L83-84)
```rust
        let from_address = parse_address(&entry.topics[2], event_source)?;
        let principal = parse_principal(&entry.topics[3], event_source)?;
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L323-338)
```rust
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
```

**File:** rs/ethereum/cketh/minter/src/blocklist.rs (L15-15)
```rust
/// ETH is not accepted from nor sent to addresses on this list.
```

**File:** rs/ethereum/cketh/minter/src/blocklist.rs (L107-109)
```rust
pub fn is_blocked(address: &Address) -> bool {
    ETH_ADDRESS_BLOCKLIST.binary_search(address).is_ok()
}
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L179-179)
```text
.. For each new event, if the `transactionHash` was not seen before (minter keeps track of minted transactions), check that the sender of the transaction is not on the blocklist and mint ckERC20 and include the transaction hash and the log entry index in the ckERC-20 mint transaction memo (ICRC-1 ledger feature). Add the `transactionHash` to the list of seen transactions kept by the minter. If the sender of the transaction was a blocked address, then the minter does not mint ckERC20, but still marks the transaction hash as seen.
```

**File:** rs/ethereum/cketh/minter/tests/cketh.rs (L222-238)
```rust
fn should_block_deposit_from_blocked_address() {
    let cketh = CkEthSetup::default();
    let from_address_blocked: Address = SAMPLE_BLOCKED_ADDRESS;

    cketh
        .deposit(DepositCkEthParams {
            from_address: from_address_blocked,
            ..Default::default()
        })
        .expect_no_mint()
        .assert_has_unique_events_in_order(&[EventPayload::InvalidDeposit {
            event_source: EventSource {
                transaction_hash: DEFAULT_DEPOSIT_TRANSACTION_HASH.to_string(),
                log_index: Nat::from(DEFAULT_DEPOSIT_LOG_INDEX),
            },
            reason: format!("blocked address {from_address_blocked}"),
        }]);
```
