### Title
Fee-on-Transfer ERC-20 Token Causes ckERC20 Over-Minting (Chain-Fusion Ledger Conservation Bug) - (File: `rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol`, `rs/ethereum/cketh/minter/ERC20DepositHelper.sol`, `rs/ethereum/cketh/minter/src/deposit.rs`)

---

### Summary

The ckERC20 deposit helper contracts (`CkDeposit.depositErc20` and `CkErc20Deposit.deposit`) emit a `ReceivedEthOrErc20` / `ReceivedErc20` event carrying the caller-supplied `amount` field **without verifying that the minter's Ethereum address actually received that amount**. The IC minter canister scrapes these events and mints exactly `event.value()` ckERC20 tokens to the beneficiary. If the underlying ERC-20 token charges a transfer fee (fee-on-transfer), the minter's Ethereum address receives `amount - fee` while the IC ledger mints `amount`, permanently breaking the 1:1 backing invariant and causing protocol insolvency.

---

### Finding Description

**Helper contract side (Ethereum):**

In `DepositHelperWithSubaccount.sol`, `depositErc20` calls `safeTransferFrom(msg.sender, minterAddress, amount)` and then unconditionally emits `ReceivedEthOrErc20(..., amount, ...)` using the caller-supplied `amount`:

```solidity
// DepositHelperWithSubaccount.sol lines 519-531
erc20Token.safeTransferFrom(
    msg.sender,
    minterAddress,
    amount
);
emit ReceivedEthOrErc20(
    erc20Address,
    msg.sender,
    amount,      // <-- always the input amount, not the actual received amount
    principal,
    subaccount
);
```

The same pattern exists in `ERC20DepositHelper.sol` (`CkErc20Deposit.deposit`, lines 500-502).

There is **no balance-before / balance-after check** to determine what the minter address actually received. For a fee-on-transfer token (e.g., a token that deducts 1% on every transfer), if a user deposits 1000 tokens, the minter receives 990 but the event records 1000.

**IC minter side (Rust):**

In `rs/ethereum/cketh/minter/src/deposit.rs`, the `mint()` function reads the scraped event and mints `event.value()` directly to the beneficiary on the ICRC-1 ledger:

```rust
// deposit.rs lines 73-81
let block_index = match client
    .transfer(TransferArg {
        ...
        amount: event.value(),   // <-- taken verbatim from the Ethereum log
    })
    .await
```

`event.value()` is populated from the `amount` field of the `ReceivedEthOrErc20` log entry (parsed in `src/eth_logs/mod.rs`, `ReceivedErc20Event.value: Erc20Value`). There is no cross-check against the minter's actual on-chain ERC-20 balance.

**End result:** For every deposit of a fee-on-transfer ERC-20 token, the IC minter mints more ckERC20 than the ERC-20 it holds. Repeated deposits drain the backing reserve, making the ckERC20 token under-collateralized and eventually insolvent when withdrawal requests exceed the minter's actual ERC-20 balance.

---

### Impact Explanation

- **Ledger conservation break**: The ckERC20 total supply on the IC ledger exceeds the ERC-20 balance held by the minter's Ethereum address. The 1:1 peg is permanently broken.
- **Protocol insolvency**: Withdrawal requests will fail once the minter's ERC-20 balance is exhausted. Users who deposited last (or who hold ckERC20 minted from fee-on-transfer deposits) cannot redeem their tokens.
- **Scope**: Affects any ckERC20 token whose underlying ERC-20 contract charges transfer fees. USDT has a fee mechanism that is currently set to zero but can be activated by its owner. Any future NNS-approved ckERC20 token with a fee-on-transfer mechanism is immediately vulnerable.

---

### Likelihood Explanation

- The NNS governance process approves new ERC-20 tokens for ckERC20 support. If any approved token activates or already has a transfer fee, the vulnerability is immediately exploitable by any user who calls `depositErc20`.
- No privileged access is required. Any unprivileged Ethereum user can call `depositErc20` on the helper contract.
- The helper contract has no whitelist enforcement; the minter enforces the token whitelist only at the log-scraping level, meaning only NNS-approved tokens trigger minting — but the bug applies to all such approved tokens equally.
- Likelihood is **medium**: it requires a fee-on-transfer token to be NNS-approved, which is a governance-controlled precondition, but the attack itself requires zero privilege once such a token exists.

---

### Recommendation

In `depositErc20` (both `DepositHelperWithSubaccount.sol` and `ERC20DepositHelper.sol`), record the minter's ERC-20 balance before and after the `safeTransferFrom` call, and emit the **actual received amount** (the difference) rather than the caller-supplied `amount`:

```solidity
function depositErc20(
    address erc20Address,
    uint256 amount,
    bytes32 principal,
    bytes32 subaccount
) public {
    require(erc20Address != ZERO_ADDRESS, "ERC20: depositErc20 from the zero address");
    IERC20 erc20Token = IERC20(erc20Address);
    uint256 balanceBefore = erc20Token.balanceOf(minterAddress);
    erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
    uint256 actualReceived = erc20Token.balanceOf(minterAddress) - balanceBefore;
    require(actualReceived > 0, "ERC20: zero amount received");
    emit ReceivedEthOrErc20(erc20Address, msg.sender, actualReceived, principal, subaccount);
}
```

This ensures the IC minter mints only what was actually received, preserving the 1:1 backing invariant regardless of the underlying token's fee behavior.

---

### Proof of Concept

1. An NNS proposal adds a fee-on-transfer ERC-20 token (e.g., a token charging 1% on every `transferFrom`) as a supported ckERC20 token.
2. Alice calls `depositErc20(tokenAddress, 1000e18, alicePrincipal, 0x)` on the `CkDeposit` helper contract.
3. The ERC-20 contract deducts 1% fee: minter receives `990e18`, but the helper emits `ReceivedEthOrErc20(..., 1000e18, ...)`.
4. The IC minter scrapes the log, reads `value = 1000e18` from `ReceivedErc20Event`, and calls `ICRC1Client.transfer(amount: 1000e18)` on the ckERC20 ledger.
5. Alice receives `1000e18` ckERC20 tokens, but the minter only holds `990e18` ERC-20 tokens.
6. After 100 such deposits, the minter holds `99_000e18` ERC-20 but the ckERC20 total supply is `100_000e18`.
7. The last ~1% of ckERC20 holders cannot redeem; withdrawal transactions revert due to insufficient ERC-20 balance at the minter address.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** rs/ethereum/cketh/minter/src/eth_logs/mod.rs (L57-75)
```rust
#[derive(Clone, Eq, PartialEq, Ord, PartialOrd, Decode, Encode)]
pub struct ReceivedErc20Event {
    #[n(0)]
    pub transaction_hash: Hash,
    #[n(1)]
    pub block_number: BlockNumber,
    #[cbor(n(2))]
    pub log_index: LogIndex,
    #[n(3)]
    pub from_address: Address,
    #[n(4)]
    pub value: Erc20Value,
    #[cbor(n(5), with = "icrc_cbor::principal")]
    pub principal: Principal,
    #[n(6)]
    pub erc20_contract_address: Address,
    #[n(7)]
    pub subaccount: Option<LedgerSubaccount>,
}
```
