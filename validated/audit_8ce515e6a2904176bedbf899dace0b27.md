Audit Report

## Title
ckERC20 Deposit Helper Contracts Emit Caller-Supplied Amount Instead of Actual Received Amount, Enabling Over-Minting for Fee-on-Transfer Tokens - (File: rs/ethereum/cketh/minter/ERC20DepositHelper.sol, rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol)

## Summary
Both ckERC20 helper contracts (`CkErc20Deposit.deposit` and `CkDeposit.depositErc20`) emit the caller-supplied `amount` parameter in the deposit event rather than the actual balance delta received by the minter address. The IC ckETH minter canister scrapes these events and mints ckERC20 tokens equal to the event-reported `amount` with no on-chain balance verification. For any ERC-20 token that charges a transfer fee, the minter receives fewer tokens than the event records, but mints the full event amount, creating unbacked ckERC20 supply and breaking the 1:1 peg.

## Finding Description
**Root cause — Solidity contracts:**

`CkErc20Deposit.deposit` in `ERC20DepositHelper.sol` (L498–503):
```solidity
function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
    IERC20 erc20Token = IERC20(erc20_address);
    erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);
    emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
}
```
`CkDeposit.depositErc20` in `DepositHelperWithSubaccount.sol` (L511–532):
```solidity
erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
emit ReceivedEthOrErc20(erc20Address, msg.sender, amount, principal, subaccount);
```
In both cases the emitted `amount` is the caller-supplied parameter, not `balanceOf(minterAddress)_after − balanceOf(minterAddress)_before`. For a fee-on-transfer ERC-20, the actual credit to the minter is `amount − fee`, but the event records `amount`.

**Root cause — IC minter Rust code:**

`parser.rs` (L86–97) reads the event data field directly as the mint value:
```rust
let [value_bytes] = parse_hex_into_32_byte_words(entry.data, event_source)?;
value: Erc20Value::from_be_bytes(value_bytes),
```
`deposit.rs` (L73–81) then mints exactly `event.value()` ckERC20 tokens:
```rust
.transfer(TransferArg {
    amount: event.value(),
    ...
})
```
`register_deposit_events` (L311–345) and `mint()` contain no step that queries the minter's actual ERC-20 balance before and after to compute the real received amount. The only guard present is a blocklist check on the sender address, which is unrelated to amount accuracy.

**Exploit flow:**
1. A fee-on-transfer ERC-20 token is whitelisted via NNS proposal (or an already-whitelisted token such as USDT activates its dormant fee mechanism).
2. Any user calls `depositErc20(tokenAddr, 1_000_000, principal, subaccount)`.
3. `safeTransferFrom` executes; due to the transfer fee, minter receives `990_000` tokens.
4. The helper emits `ReceivedEthOrErc20(..., 1_000_000, ...)`.
5. The IC minter scrapes the log, parses `value = 1_000_000`, and mints `1_000_000` ckERC20.
6. The depositor holds `1_000_000` ckERC20 backed by only `990_000` ERC-20 tokens.
7. Repeated deposits accumulate unbacked supply; the last withdrawers cannot redeem.

**Existing checks reviewed and found insufficient:**
- The NNS whitelist prevents unsupported tokens from being processed, but does not prevent a whitelisted token from having or activating transfer fees.
- `SafeERC20.safeTransferFrom` reverts on failure but does not return the actual transferred amount.
- No balance-delta measurement exists anywhere in the deposit pipeline.

## Impact Explanation
If a whitelisted ERC-20 token charges a transfer fee, every deposit mints more ckERC20 than the minter holds in ERC-20 reserves. Over time, total ckERC20 supply exceeds the minter's ERC-20 balance. When users attempt to withdraw ckERC20 back to ERC-20, the minter cannot fulfill all requests — the last withdrawers receive nothing. This is a direct ledger conservation break and constitutes **illegal minting of in-scope chain-key/ledger assets**, matching the High impact class: "Significant Chain Fusion, ck-token, ledger … security impact with concrete user or protocol harm." The scale of loss is bounded by the total deposits made while the fee-on-transfer condition holds; for a high-volume token this could be substantial.

## Likelihood Explanation
The exploit requires a whitelisted token to have fee-on-transfer behavior. Currently whitelisted tokens (USDC, USDT, LINK, PEPE, SHIB, UNI, WBTC, wstETH, XAUt, EURC, OCT) do not currently charge transfer fees. However: (1) USDT's ERC-20 contract contains a dormant fee mechanism (`basisPointsRate`, `maximumFee`) that Tether can activate without any IC governance action; (2) proxy-pattern tokens such as USDC are upgradeable by their issuers; (3) future NNS proposals may add tokens whose fee behavior is not scrutinized. Once the precondition is met, the exploit path is fully unprivileged — any depositor triggers it with a standard deposit call. **Likelihood: Low-Medium.**

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
1. Deploy a local fork or PocketIC environment with the ckETH minter canister and a custom ERC-20 token contract that deducts a 1% fee on every `transferFrom` call.
2. Submit an NNS proposal (or directly call `add_ckerc20_token` in a test environment) to whitelist the fee-on-transfer token.
3. Call `depositErc20(feeTokenAddr, 1_000_000, principal, subaccount)` on `CkDeposit`.
4. Observe: minter's ERC-20 balance increases by `990_000`; the emitted event records `1_000_000`; the IC minter mints `1_000_000` ckERC20.
5. Assert `ckERC20.totalSupply() > minter.erc20BalanceOf(feeTokenAddr)` — invariant broken.
6. Repeat deposits to accumulate unbacked supply, then attempt a full withdrawal to demonstrate that the last withdrawer receives zero ERC-20 tokens.