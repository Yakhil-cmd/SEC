### Title
Blocklist Bypass via Proxy Contract: `is_blocked()` Checks Only `msg.sender` (`from_address`), Not Ultimate Fund Source — (`rs/ethereum/cketh/minter/src/deposit.rs`)

---

### Summary

The ckETH minter's deposit blocklist check in `register_deposit_events()` only evaluates `event.from_address()`, which maps directly to `msg.sender` in the Solidity helper contract. A sanctioned address can route funds through an intermediary (proxy) contract so that `from_address` in the emitted log is the proxy's address (not on the blocklist), while the sanctioned EOA is the ultimate source of funds. The minter then mints ckETH/ckERC20 to the attacker-controlled IC principal.

---

### Finding Description

**Step 1 — Solidity helper emits `msg.sender` as `from_address`:**

In `EthDepositHelper.sol`:
```solidity
function deposit(bytes32 _principal) public payable {
    emit ReceivedEth(msg.sender, msg.value, _principal);
    cketh_minter_main_address.transfer(msg.value);
}
``` [1](#0-0) 

In `DepositHelperWithSubaccount.sol` (the current production contract):
```solidity
function depositEth(bytes32 principal, bytes32 subaccount) public payable {
    emit ReceivedEthOrErc20(ZERO_ADDRESS, msg.sender, msg.value, principal, subaccount);
    minterAddress.transfer(msg.value);
}
``` [2](#0-1) 

`msg.sender` is the **immediate caller** of the helper contract — not `tx.origin` (the original EOA). If a proxy contract calls `depositEth`, `msg.sender` = proxy address.

**Step 2 — Parser reads `from_address` from log topic:**

The minter parses `from_address` directly from `topics[2]` of the log entry: [3](#0-2) 

**Step 3 — Blocklist check only tests `from_address`:**

```rust
if crate::blocklist::is_blocked(&event.from_address()) {
``` [4](#0-3) 

`is_blocked()` performs a binary search on `ETH_ADDRESS_BLOCKLIST` using only the `from_address` field: [5](#0-4) 

The `principal` (IC beneficiary) is never checked against any blocklist. [6](#0-5) 

---

### Impact Explanation

A sanctioned Ethereum address (present in `ETH_ADDRESS_BLOCKLIST`) can:

1. Deploy or use any proxy contract not on the blocklist.
2. Fund the proxy with ETH (or approve it for ERC-20).
3. Have the proxy call `depositEth(principal, subaccount)` on the helper contract, encoding the sanctioned entity's IC principal.
4. The helper emits `ReceivedEthOrErc20(ZERO_ADDRESS, proxy_address, value, principal, subaccount)`.
5. The minter sees `from_address = proxy_address`, `is_blocked(proxy_address)` returns `false`.
6. ckETH is minted to the sanctioned entity's IC account.

The stated invariant — *"ETH is not accepted from nor sent to addresses on this list"* — is violated. [7](#0-6) 

---

### Likelihood Explanation

The attack requires no privileged access, no key compromise, and no consensus manipulation. Any sanctioned address can deploy a trivial forwarding contract (or use an existing one like a multisig or DeFi router) and call the helper. The proxy contract itself is not on the blocklist and never will be unless explicitly added. This is a straightforward, low-cost bypass executable by any technically capable actor.

---

### Recommendation

The `is_blocked()` check should also be applied to the **transaction originator** (`tx.origin` in Solidity), not just `msg.sender`. The helper contract should emit `tx.origin` as an additional field, or the minter should cross-reference the Ethereum transaction's `from` field (the EOA that signed the transaction) via `eth_getTransactionByHash`. Alternatively, the blocklist check should be extended to also block deposits where the `principal` maps to a known-sanctioned IC identity, though this requires a separate IC-side blocklist.

---

### Proof of Concept

Construct a `ReceivedEthEvent` where:
- `from_address` = any address **not** in `ETH_ADDRESS_BLOCKLIST` (simulating a proxy)
- `principal` = the IC principal of the sanctioned user

```rust
// State-machine test sketch
let proxy_address = Address::new(hex!("deadbeef...")); // not on blocklist
let sanctioned_principal = Principal::from_text("...").unwrap();

let event = ReceivedEthEvent {
    from_address: proxy_address,
    principal: sanctioned_principal,
    value: Wei::from(1_000_000_000_000_000_u128),
    // ... other fields
};

// register_deposit_events will call is_blocked(&proxy_address) -> false
// and proceed to mint ckETH to sanctioned_principal
register_deposit_events(scraping_id, vec![event.into()], vec![]);

// Assert: events_to_mint is non-empty (deposit was accepted)
assert!(read_state(State::has_events_to_mint));
```

The deposit is accepted and ckETH is minted to the sanctioned entity's IC account, bypassing the compliance control entirely. [8](#0-7)

### Citations

**File:** rs/ethereum/cketh/minter/EthDepositHelper.sol (L32-35)
```text
    function deposit(bytes32 _principal) public payable {
        emit ReceivedEth(msg.sender, msg.value, _principal);
        cketh_minter_main_address.transfer(msg.value);
    }
```

**File:** rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol (L503-506)
```text
    function depositEth(bytes32 principal, bytes32 subaccount) public payable {
        emit ReceivedEthOrErc20(ZERO_ADDRESS, msg.sender, msg.value, principal, subaccount);
        minterAddress.transfer(msg.value);
    }
```

**File:** rs/ethereum/cketh/minter/src/eth_logs/parser.rs (L124-126)
```rust
        let erc20_contract_address = parse_address(&entry.topics[1], event_source)?;
        let from_address = parse_address(&entry.topics[2], event_source)?;
        let principal = parse_principal(&entry.topics[3], event_source)?;
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L316-342)
```rust
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
```

**File:** rs/ethereum/cketh/minter/src/blocklist.rs (L15-17)
```rust
/// ETH is not accepted from nor sent to addresses on this list.
/// NOTE: Keep it sorted!
const ETH_ADDRESS_BLOCKLIST: &[Address] = &[
```

**File:** rs/ethereum/cketh/minter/src/blocklist.rs (L107-109)
```rust
pub fn is_blocked(address: &Address) -> bool {
    ETH_ADDRESS_BLOCKLIST.binary_search(address).is_ok()
}
```
