### Title
Fee-on-Transfer ERC20 Token Deposit Mints Unbacked ckERC20 Tokens — (`rs/ethereum/cketh/minter/ERC20DepositHelper.sol`, `rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol`, `rs/ethereum/cketh/minter/src/deposit.rs`)

---

### Summary

The ckERC20 deposit helper contracts emit the caller-supplied `amount` parameter in the `ReceivedErc20` / `ReceivedEthOrErc20` event, and the IC minter mints exactly that event-logged value as ckERC20 tokens. For fee-on-transfer (deflationary) ERC20 tokens, the actual amount credited to the minter's Ethereum address is `amount − fee`, while the event records `amount`. The minter therefore mints more ckERC20 than it holds in ERC20 collateral, breaking the 1:1 backing invariant and eventually making legitimate withdrawals impossible.

---

### Finding Description

**Step 1 — Helper contracts emit the input `amount`, not the received amount.**

`ERC20DepositHelper.sol` (`deposit`):

```solidity
erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);
emit ReceivedErc20(erc20_address, msg.sender, amount, principal);   // ← always `amount`
```

`DepositHelperWithSubaccount.sol` (`depositErc20`):

```solidity
erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
emit ReceivedEthOrErc20(erc20Address, msg.sender, amount, principal, subaccount);  // ← always `amount`
```

For a fee-on-transfer token, `safeTransferFrom` succeeds but the minter address receives only `amount − fee`. The emitted event still carries the full `amount`.

**Step 2 — The IC minter parses the event value and mints it verbatim.**

`rs/ethereum/cketh/minter/src/eth_logs/parser.rs` (`ReceivedErc20LogParser::parse_log`):

```rust
let [value_bytes] = parse_hex_into_32_byte_words(entry.data, event_source)?;
Ok(ReceivedErc20Event {
    value: Erc20Value::from_be_bytes(value_bytes),   // ← raw event field
    ...
})
```

`rs/ethereum/cketh/minter/src/deposit.rs` (`mint`):

```rust
client.transfer(TransferArg {
    amount: event.value(),   // ← mints the event-logged amount, not actual balance change
    ...
}).await
```

The minter never queries the actual ERC20 balance change at its Ethereum address; it trusts the event log unconditionally.

**Step 3 — The whitelist is the only guard, and it is not enforced in code.**

The documentation explicitly states:

> "Note that the helper smart contract does not enforce any whitelist of allowed ERC20 tokens. This is enforced by the minter, which fetches logs only for the supported ERC20 tokens."

The minter's whitelist (`ckerc20_tokens`) is populated via `add_ckerc20_token`, callable only by the ledger suite orchestrator, which is controlled by NNS proposals. There is no on-chain or in-code check that a whitelisted token is not fee-on-transfer. A token that is standard at listing time can later upgrade its contract to add transfer fees (many ERC20 tokens are upgradeable proxies), or the NNS voters may simply not detect the fee mechanism during review.

---

### Impact Explanation

**Ledger conservation break (chain-fusion mint/burn bug):** For every deposit of a fee-on-transfer token, the minter mints `amount` ckERC20 but holds only `amount − fee` ERC20. The ckERC20 total supply grows faster than the ERC20 reserve. When users later call `withdraw_erc20`, the minter constructs an Ethereum transaction for the full ckERC20 burn amount, but its Ethereum address lacks sufficient ERC20 balance. Withdrawals fail or drain reserves belonging to other users, causing direct loss of funds for legitimate ckERC20 holders.

---

### Likelihood Explanation

**Medium.** Adding a new ckERC20 token requires an NNS proposal, which is a governance-controlled gate. However:
- Many popular ERC20 tokens are upgradeable proxies; a token that is standard at listing time can silently add transfer fees in a later upgrade (e.g., USDT has a configurable fee mechanism).
- NNS voters review token metadata and contract addresses, not bytecode-level transfer semantics.
- The codebase already lists ckPEPE, ckWBTC, ckUNI, ckEURC — each required a separate NNS proposal, and the review process has no automated fee-on-transfer detection.
- No code-level guard exists; the vulnerability is latent for every currently whitelisted token that could later enable fees.

---

### Recommendation

1. **Measure actual balance change in the helper contracts.** Record the minter's ERC20 balance before and after `safeTransferFrom` and emit the delta, not the input `amount`:

```solidity
uint256 before = IERC20(erc20Address).balanceOf(minterAddress);
erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
uint256 received = IERC20(erc20Address).balanceOf(minterAddress) - before;
emit ReceivedEthOrErc20(erc20Address, msg.sender, received, principal, subaccount);
```

2. **Alternatively, enforce a no-fee-on-transfer invariant in the minter.** When scraping logs, the minter could cross-check the ERC20 balance of its Ethereum address against the cumulative minted supply and halt minting if a discrepancy is detected.

3. **Add an explicit check during token onboarding.** The ledger suite orchestrator or the NNS proposal tooling should verify that `transferFrom(addr, addr, 1)` returns a balance delta of exactly `1` before a token is whitelisted.

---

### Proof of Concept

1. A fee-on-transfer ERC20 token `FEE` (2% fee on every transfer) is added to the ckERC20 whitelist via NNS proposal.
2. Attacker calls `approve(helperContract, 1000)` on `FEE`.
3. Attacker calls `depositErc20(FEE, 1000, principal, subaccount)` on `DepositHelperWithSubaccount`.
4. `safeTransferFrom` executes: minter receives `980` FEE (2% fee deducted). The helper emits `ReceivedEthOrErc20(..., 1000, ...)`.
5. The IC minter scrapes the log, parses `value = 1000`, and mints `1000 ckFEE` to the attacker.
6. Attacker now holds `1000 ckFEE` backed by only `980 FEE` in the minter's Ethereum address.
7. Attacker calls `withdraw_erc20` for `1000 ckFEE`. The minter burns `1000 ckFEE` and attempts to send `1000 FEE` to Ethereum — but only `980 FEE` is available, causing the withdrawal to fail or steal `20 FEE` from other depositors' reserves.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** rs/ethereum/cketh/minter/src/eth_logs/parser.rs (L86-103)
```rust
        let [value_bytes] = parse_hex_into_32_byte_words(entry.data, event_source)?;
        let EventSource {
            transaction_hash,
            log_index,
        } = event_source;

        Ok(ReceivedErc20Event {
            transaction_hash,
            block_number,
            log_index,
            from_address,
            value: Erc20Value::from_be_bytes(value_bytes),
            principal,
            erc20_contract_address,
            subaccount: None,
        }
        .into())
    }
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L73-81)
```rust
        let block_index = match client
            .transfer(TransferArg {
                from_subaccount: None,
                to: event.beneficiary(),
                fee: None,
                created_at_time: None,
                memo: Some((&event).into()),
                amount: event.value(),
            })
```
