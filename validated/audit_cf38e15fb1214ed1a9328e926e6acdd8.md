Audit Report

## Title
ckERC20 Deposit Helper Contracts Emit Requested Amount Instead of Actual Received Amount, Enabling Over-Minting for Fee-on-Transfer Tokens - (File: rs/ethereum/cketh/minter/ERC20DepositHelper.sol, rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol)

## Summary
Both ckERC20 helper contracts (`CkErc20Deposit.deposit` and `CkDeposit.depositErc20`) emit the caller-supplied `amount` parameter in the deposit event rather than the actual balance delta received by the minter address. The IC ckETH minter canister scrapes these events and mints ckERC20 tokens equal to the event-reported `amount` with no on-chain balance verification. For any ERC-20 token that charges a transfer fee, the minter's Ethereum address receives fewer tokens than the event records, but the IC minter mints the full event amount, creating unbacked ckERC20 supply and breaking the 1:1 peg.

## Finding Description
**Root cause — `ERC20DepositHelper.sol` (`CkErc20Deposit.deposit`, L498–503):**
```solidity
function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
    IERC20 erc20Token = IERC20(erc20_address);
    erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);
    emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
}
```
The emitted `amount` is the caller-supplied parameter, not `balanceOf(minter)_after − balanceOf(minter)_before`.

**Root cause — `DepositHelperWithSubaccount.sol` (`CkDeposit.depositErc20`, L511–532):**
```solidity
erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
emit ReceivedEthOrErc20(erc20Address, msg.sender, amount, principal, subaccount);
```
Same pattern: emitted value is the requested `amount`, not the actual credit.

**IC minter log parser (`parser.rs`, L86–97):** reads the event data field directly as the mint value:
```rust
let [value_bytes] = parse_hex_into_32_byte_words(entry.data, event_source)?;
value: Erc20Value::from_be_bytes(value_bytes),
```

**IC minter mint path (`deposit.rs`, L73–81):** mints exactly `event.value()` with no balance reconciliation:
```rust
.transfer(TransferArg {
    amount: event.value(),
    ...
})
```

**No mitigation exists.** Neither `scrape_logs`, `register_deposit_events`, nor `mint` queries the minter's actual ERC-20 balance before and after to compute the real received amount. The only guard is a blocklist check on the sender address, which is unrelated to fee-on-transfer accounting.

**Exploit flow:**
1. A fee-on-transfer ERC-20 token is whitelisted (e.g., USDT, which has a dormant fee mechanism currently set to 0% but activatable by Tether, or any future NNS-approved token).
2. Attacker calls `depositErc20(tokenAddr, 1_000_000, principal, subaccount)`.
3. `safeTransferFrom` credits the minter with `990_000` tokens (1% fee deducted).
4. Helper emits `ReceivedEthOrErc20(..., 1_000_000, ...)`.
5. IC minter scrapes the log, parses `value = 1_000_000`, mints `1_000_000` ckERC20 to the attacker.
6. Attacker holds `1_000_000` ckERC20 backed by only `990_000` ERC-20 tokens.
7. Repeated deposits drain the reserve; the final `10_000` ckERC20 per cycle are permanently unbacked.

## Impact Explanation
**Illegal minting / protocol insolvency of an in-scope ck-token asset.** The total ckERC20 supply exceeds the minter's ERC-20 reserve. When users attempt to withdraw ckERC20 back to ERC-20, the minter cannot fulfill all requests — the last withdrawers receive nothing. This is a direct ledger conservation break for the ckERC20 token, matching the allowed Critical/High impact class: *"Theft, permanent loss, illegal minting, or protocol insolvency involving in-scope chain-key/ledger assets"* and *"Significant Chain Fusion, ck-token, ledger … security impact with concrete user or protocol harm."*

Severity: **High** — the impact is severe but requires the precondition of a whitelisted token activating fee-on-transfer behavior.

## Likelihood Explanation
The precondition is realistic rather than theoretical:
- **USDT** (`0xdAC17F958D2ee523a2206206994597C13D831ec7`) is already whitelisted as ckUSDT and contains a dormant fee mechanism (`basisPointsRate`, `maximumFee`) that Tether can activate via a contract owner call without any IC governance action.
- **USDC** is an upgradeable proxy; Circle could introduce fee-on-transfer in a future upgrade.
- Once the precondition is met, the exploit requires no attacker privilege beyond submitting a normal `depositErc20` transaction — fully unprivileged.
- The exploit is repeatable and cumulative; each deposit cycle widens the reserve shortfall.

## Recommendation
Both helper contracts should measure the actual balance delta and emit that as the deposit amount:

```solidity
function depositErc20(address erc20Address, uint256 amount, bytes32 principal, bytes32 subaccount) public {
    require(erc20Address != ZERO_ADDRESS, "ERC20: depositErc20 from the zero address");
    IERC20 erc20Token = IERC20(erc20Address);
    uint256 balanceBefore = erc20Token.balanceOf(minterAddress);
    erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
    uint256 actualAmount = erc20Token.balanceOf(minterAddress) - balanceBefore;
    emit ReceivedEthOrErc20(erc20Address, msg.sender, actualAmount, principal, subaccount);
}
```
Apply the same pattern to `CkErc20Deposit.deposit` in `ERC20DepositHelper.sol`. This ensures the IC minter only mints ckERC20 equal to the tokens it actually received.

## Proof of Concept
**Deterministic integration test plan (using the existing PocketIC/ckerc20 test harness in `rs/ethereum/cketh/minter/tests/ckerc20.rs`):**

1. Deploy a custom ERC-20 token contract that deducts a 1% fee on every `transferFrom` (crediting the fee to a separate address).
2. Register this token as a supported ckERC20 token in the minter state.
3. Call `depositErc20(tokenAddr, 1_000_000, principal, subaccount)` via the helper contract.
4. Assert that the helper contract emits `ReceivedEthOrErc20(..., 1_000_000, ...)` (the requested amount).
5. Assert that the minter's ERC-20 balance increased by only `990_000`.
6. Allow the minter to scrape logs and mint.
7. Assert that the ckERC20 ledger minted `1_000_000` to the beneficiary — **10_000 more than the minter holds**.
8. Repeat N times and assert that `total_ckERC20_supply > minter_ERC20_balance`, confirming the reserve shortfall grows linearly.