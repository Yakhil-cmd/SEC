Looking at the exact code path:

### Title
Blocklist Check Bypassed via Proxy Contract — `msg.sender` in Helper Contract Reflects Proxy, Not Sanctioned Originator - (`rs/ethereum/cketh/minter/src/deposit.rs`, `rs/ethereum/cketh/minter/src/blocklist.rs`)

---

### Summary

The ckETH/ckERC20 minter's blocklist enforcement checks only the `owner` field emitted in the `ReceivedEth`/`ReceivedErc20`/`ReceivedEthOrErc20` event log, which is `msg.sender` inside the helper smart contract. When a sanctioned address routes a deposit through an intermediary (proxy) smart contract, `msg.sender` inside the helper is the proxy's address — not the sanctioned originator. Since the proxy is not on the blocklist, `is_blocked` returns `false` and minting proceeds.

---

### Finding Description

**Step 1 — Helper contract emits `msg.sender` as `owner`:**

In `DepositHelperWithSubaccount.sol`, both deposit functions unconditionally emit `msg.sender`:

```solidity
function depositEth(bytes32 principal, bytes32 subaccount) public payable {
    emit ReceivedEthOrErc20(ZERO_ADDRESS, msg.sender, msg.value, principal, subaccount);
    ...
}

function depositErc20(...) public {
    erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
    emit ReceivedEthOrErc20(erc20Address, msg.sender, amount, principal, subaccount);
}
``` [1](#0-0) 

If a proxy contract calls `depositEth`/`depositErc20`, `msg.sender` = proxy address, not the sanctioned EOA that initiated the chain.

**Step 2 — Parser reads `from_address` from `topics[2]` (the `owner` field):**

All three log parsers extract `from_address` from `entry.topics[2]`, which is the `owner` field — i.e., `msg.sender` in the helper contract: [2](#0-1) [3](#0-2) 

**Step 3 — Blocklist check operates only on this `from_address`:**

`register_deposit_events` calls `is_blocked(&event.from_address())`. There is no check against `tx.origin` or any other field that would identify the ultimate Ethereum transaction signer: [4](#0-3) 

**Step 4 — `is_blocked` is a static list lookup:** [5](#0-4) 

A proxy address not on the list returns `false`, and `event.into_deposit()` is called, triggering minting.

---

### Impact Explanation

A sanctioned entity can receive ckETH or ckERC20 tokens by:

1. Deploying (or reusing) any smart contract not on the blocklist as a proxy.
2. Sending ETH/ERC20 to the proxy (or pre-approving it for ERC20).
3. Having the proxy call `depositEth`/`depositErc20` on the official helper contract with the sanctioned entity's IC principal as the recipient.
4. The minter observes the event with `owner = proxy_address`, passes `is_blocked(proxy_address) == false`, and mints ckETH/ckERC20 to the sanctioned entity's IC account.

The invariant stated in the comment — *"ETH is not accepted from nor sent to addresses on this list"* — is violated. [6](#0-5) 

---

### Likelihood Explanation

The attack requires no privileged access, no key compromise, and no protocol-level attack. Any Ethereum user can deploy a forwarding contract in a single transaction. The technique is well-known (it is the same mechanism Tornado Cash exploits). The only cost is Ethereum gas. The sanctioned entity does not even need to interact with the IC directly — the IC principal encoded in the deposit can belong to any account they control.

---

### Recommendation

The minter cannot reliably enforce sanctions purely at the `msg.sender` layer of the helper contract because Ethereum does not prevent contract-to-contract calls. Mitigations to consider:

1. **Check `tx.origin` in the helper contract** — emit `tx.origin` as a second address field in the event and have the minter check both. Note: `tx.origin` is the EOA that signed the Ethereum transaction and cannot be spoofed by a proxy.
2. **Emit and check both `msg.sender` and `tx.origin`** — block the deposit if either is on the blocklist.
3. **Document the limitation** — if the design intentionally only screens the immediate caller, document that proxy-based bypasses are out of scope and accepted risk.

Option 1/2 requires a new helper contract deployment and a minter upgrade to parse the additional field.

---

### Proof of Concept

```solidity
// Proxy contract (not on blocklist)
contract Proxy {
    function forwardDeposit(
        address helper,
        bytes32 principal,
        bytes32 subaccount
    ) external payable {
        // msg.sender inside CkDeposit.depositEth will be address(this), not tx.origin
        CkDeposit(helper).depositEth{value: msg.value}(principal, subaccount);
    }
}
```

1. Deploy `Proxy` from any address (including `SAMPLE_BLOCKED_ADDRESS`).
2. Call `proxy.forwardDeposit{value: 1 ether}(helperAddress, encodedPrincipal, 0x00...)` from `SAMPLE_BLOCKED_ADDRESS`.
3. The helper emits `ReceivedEthOrErc20(address(0), proxy_address, 1 ether, encodedPrincipal, 0x00...)`.
4. The minter scrapes the log, calls `is_blocked(proxy_address)` → `false`.
5. `register_deposit_events` calls `event.into_deposit()` → minting proceeds.
6. Assert: `is_blocked(SAMPLE_BLOCKED_ADDRESS) == true`, `is_blocked(proxy_address) == false`, and ckETH is minted to the sanctioned entity's IC principal.

### Citations

**File:** rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol (L503-531)
```text
    function depositEth(bytes32 principal, bytes32 subaccount) public payable {
        emit ReceivedEthOrErc20(ZERO_ADDRESS, msg.sender, msg.value, principal, subaccount);
        minterAddress.transfer(msg.value);
    }

    /**
     * @dev Emits the `ReceivedEthOrErc20` event if the transfer succeeds.
     */
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
```

**File:** rs/ethereum/cketh/minter/src/eth_logs/parser.rs (L83-84)
```rust
        let from_address = parse_address(&entry.topics[2], event_source)?;
        let principal = parse_principal(&entry.topics[3], event_source)?;
```

**File:** rs/ethereum/cketh/minter/src/eth_logs/parser.rs (L124-126)
```rust
        let erc20_contract_address = parse_address(&entry.topics[1], event_source)?;
        let from_address = parse_address(&entry.topics[2], event_source)?;
        let principal = parse_principal(&entry.topics[3], event_source)?;
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L323-341)
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
        } else {
            mutate_state(|s| process_event(s, event.into_deposit()));
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
