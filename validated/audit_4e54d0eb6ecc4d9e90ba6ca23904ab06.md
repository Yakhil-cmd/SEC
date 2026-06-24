Audit Report

## Title
Blocklist Enforcement Bypassed via Proxy Contract — `msg.sender` in Helper Reflects Proxy, Not Sanctioned Originator - (File: `rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol`, `rs/ethereum/cketh/minter/src/deposit.rs`, `rs/ethereum/cketh/minter/src/blocklist.rs`)

## Summary

The ckETH/ckERC20 minter's sanctions blocklist is enforced exclusively against the `from_address` field parsed from emitted event logs, which corresponds to `msg.sender` inside the helper contract. When a sanctioned EOA routes a deposit through any intermediary smart contract (proxy), `msg.sender` inside the helper is the proxy's address. Since the proxy is not on the blocklist, `is_blocked` returns `false` and minting proceeds to the sanctioned entity's IC principal, directly violating the stated invariant that "ETH is not accepted from nor sent to addresses on this list."

## Finding Description

**Root cause — Helper emits `msg.sender` unconditionally:**

`DepositHelperWithSubaccount.sol` lines 503–531 show both `depositEth` and `depositErc20` emit `msg.sender` as the depositor identity field. When called by a proxy contract, `msg.sender` is the proxy address, not the EOA that originated the transaction.

**Parser reads this field as `from_address`:**

`ReceivedEthOrErc20LogParser::parse_log` (parser.rs L124–126) and `ReceivedErc20LogParser::parse_log` (parser.rs L82–84) both extract `from_address` from `entry.topics[2]`, which is the `msg.sender`-derived field. `ReceivedEthLogParser::parse_log` reads it from `entry.topics[1]` (parser.rs L45) — same semantic field, same problem.

**Blocklist check operates only on this `from_address`:**

`register_deposit_events` in `deposit.rs` L323 calls `crate::blocklist::is_blocked(&event.from_address())`. There is no check against `tx.origin` or any other field identifying the ultimate transaction signer. If the proxy address is not on the blocklist, the `else` branch at L340 calls `event.into_deposit()`, triggering minting.

**`is_blocked` is a static binary search:**

`blocklist.rs` L107–109 performs `ETH_ADDRESS_BLOCKLIST.binary_search(address).is_ok()`. A freshly deployed proxy address is not in this list and returns `false`.

**Existing guards are insufficient:** The only guard is the single `is_blocked` call on `from_address`. There is no check on the Ethereum transaction sender (`tx.origin`), no on-chain restriction in the helper contract preventing contract-to-contract calls, and no secondary validation in the minter.

## Impact Explanation

A sanctioned entity can receive ckETH or ckERC20 tokens by encoding their IC principal as the deposit recipient and routing the Ethereum-side deposit through any unlisted proxy contract. This constitutes illegal minting of in-scope chain-key assets (ckETH/ckERC20) to a sanctioned party, directly violating the protocol's stated sanctions enforcement invariant. The impact maps to the Critical/High bounty category: "Theft, permanent loss, illegal minting, or protocol insolvency involving in-scope chain-key/ledger assets." Severity is High at minimum; if exploited at scale it reaches Critical given no upper bound on deposit size.

## Likelihood Explanation

The attack requires no privileged access, no key compromise, and no protocol-level attack. Deploying a forwarding proxy contract costs a single Ethereum transaction (a few dollars in gas). The technique is well-known and requires only basic Solidity knowledge. The sanctioned entity does not need to interact with the IC directly — they only need to control the IC principal encoded in the deposit. The attack is repeatable indefinitely until the proxy address is manually added to the blocklist (which requires a minter upgrade).

## Recommendation

1. **Emit and check `tx.origin` in addition to `msg.sender`:** Modify the helper contract to include `tx.origin` as an additional indexed field in the `ReceivedEthOrErc20` event. Update the minter's log parser to extract this field and call `is_blocked` on both `from_address` (msg.sender) and the `tx.origin` field, blocking the deposit if either is sanctioned.
2. **Add a contract-call guard in the helper:** Add `require(msg.sender == tx.origin, "no contract calls")` to `depositEth` and `depositErc20` if proxy deposits are not an intended use case. This prevents the bypass entirely at the Ethereum layer.
3. **Document accepted risk:** If proxy deposits are intentional, explicitly document that the blocklist only screens the immediate caller and that proxy-based bypasses are accepted risk.

Options 1 and 2 require a new helper contract deployment and a minter upgrade to parse the additional field.

## Proof of Concept

```solidity
// Proxy contract (not on blocklist)
contract Proxy {
    function forwardDeposit(
        address helper,
        bytes32 principal,
        bytes32 subaccount
    ) external payable {
        // msg.sender inside depositEth will be address(this), not tx.origin
        IDepositHelper(helper).depositEth{value: msg.value}(principal, subaccount);
    }
}
```

1. Deploy `Proxy` from `SAMPLE_BLOCKED_ADDRESS` (or any sanctioned EOA).
2. Call `proxy.forwardDeposit{value: 1 ether}(helperAddress, encodedPrincipal, 0x00...)` from `SAMPLE_BLOCKED_ADDRESS`.
3. The helper emits `ReceivedEthOrErc20(address(0), proxy_address, 1 ether, encodedPrincipal, 0x00...)`.
4. The minter scrapes the log; `event.from_address()` = `proxy_address`.
5. `is_blocked(proxy_address)` → `false` (proxy not on list).
6. `register_deposit_events` calls `event.into_deposit()` → minting proceeds.
7. Assert: `is_blocked(SAMPLE_BLOCKED_ADDRESS) == true`, `is_blocked(proxy_address) == false`, and ckETH is minted to the sanctioned entity's IC principal.

A deterministic integration test can be written using a local Anvil fork: deploy the helper, deploy the proxy, call `forwardDeposit` from a hardcoded blocked address, scrape the resulting log through the minter's `register_deposit_events`, and assert that minting was not blocked.