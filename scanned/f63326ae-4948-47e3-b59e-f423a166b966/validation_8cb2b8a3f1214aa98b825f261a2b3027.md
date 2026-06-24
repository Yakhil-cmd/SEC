### Title
Fee-on-Transfer ERC-20 Token Over-Minting in ckERC20 Deposit Helper Contracts - (File: `rs/ethereum/cketh/minter/ERC20DepositHelper.sol`, `rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol`)

---

### Summary

Both ckERC20 deposit helper contracts (`CkErc20Deposit.deposit()` and `CkDeposit.depositErc20()`) call `safeTransferFrom(msg.sender, minterAddress, amount)` and then emit a deposit event carrying the caller-supplied `amount` rather than the actual tokens received at `minterAddress`. For fee-on-transfer ERC-20 tokens the minter's Ethereum address receives `amount - fee`, but the emitted event value is `amount`. The IC minter scrapes these events and mints ckERC20 equal to the event value, permanently over-minting relative to the ERC-20 tokens it actually holds, breaking the 1:1 backing invariant.

---

### Finding Description

**`CkErc20Deposit.deposit()` in `ERC20DepositHelper.sol`:**

```solidity
function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
    IERC20 erc20Token = IERC20(erc20_address);
    erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);
    // ↑ for fee-on-transfer tokens, minter receives (amount - fee), not amount
    emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
    // ↑ event carries `amount`, not actual received
}
``` [1](#0-0) 

**`CkDeposit.depositErc20()` in `DepositHelperWithSubaccount.sol`:**

```solidity
function depositErc20(address erc20Address, uint256 amount, ...) public {
    erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
    // ↑ for fee-on-transfer tokens, minter receives (amount - fee)
    emit ReceivedEthOrErc20(erc20Address, msg.sender, amount, principal, subaccount);
    // ↑ event carries `amount`, not actual received
}
``` [2](#0-1) 

The IC minter's `mint()` function in `rs/ethereum/cketh/minter/src/deposit.rs` reads these events and mints ckERC20 tokens equal to `event.value()`, which is sourced directly from the event's `amount` field:

```rust
amount: event.value(),   // taken verbatim from the Ethereum log
``` [3](#0-2) 

The minter's internal ERC-20 balance accounting (`Erc20Balances::erc20_add`) is also updated with the full event `amount`, not the actual on-chain balance: [4](#0-3) 

This means both the ckERC20 ledger total supply and the minter's internal `erc20_balances` tracking diverge from the actual ERC-20 tokens held at the minter's Ethereum address.

---

### Impact Explanation

**Classification:** Chain-fusion mint/burn conservation bug (ledger conservation bug).

For every deposit of a fee-on-transfer ERC-20 token with transfer fee `f`:
- Minter's Ethereum address receives: `amount - f`
- ckERC20 minted to user: `amount`
- Discrepancy per deposit: `f` ckERC20 tokens unbacked by real ERC-20

Over multiple deposits the shortfall accumulates. When users later withdraw ckERC20 → ERC-20, the minter constructs an Ethereum transaction sending `withdrawal_amount` of ERC-20 tokens from its address. If the cumulative shortfall exceeds the minter's actual ERC-20 holdings, the Ethereum transaction will revert (insufficient balance), causing withdrawal failures for legitimate users. The minter's internal `erc20_balances` will also show a higher balance than actually held, corrupting the accounting used by `get_minter_info` and the minter dashboard.

**Impact: Medium** — Breaks the 1:1 backing invariant of ckERC20 tokens; legitimate withdrawal requests can fail; minter accounting is permanently corrupted for affected tokens.

---

### Likelihood Explanation

**Likelihood: Medium.**

Currently supported ckERC20 tokens (USDC, USDT, etc.) are standard ERC-20 tokens without transfer fees, so the vulnerability is not actively exploitable today. However:

1. The helper contracts enforce **no whitelist** — any ERC-20 address can be passed to `deposit()` / `depositErc20()`.
2. The minter's supported token list is controlled by NNS governance proposals. If any fee-on-transfer token (e.g., PAXG, STA, or a rebasing token) is ever added via an NNS proposal, the vulnerability is immediately exploitable by any unprivileged user.
3. The helper contracts are **immutable** once deployed on Ethereum — the fix requires deploying new helper contracts and migrating the minter configuration via another NNS proposal.
4. The attacker entry path requires only a standard Ethereum transaction calling `deposit()` or `depositErc20()` with a fee-on-transfer ERC-20 address — no privileged access needed.

---

### Recommendation

Both helper contracts should measure the actual received amount using a balance-before/after pattern and emit that value in the event:

```solidity
function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
    IERC20 erc20Token = IERC20(erc20_address);
    uint256 balanceBefore = erc20Token.balanceOf(cketh_minter_main_address);
    erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);
    uint256 actualReceived = erc20Token.balanceOf(cketh_minter_main_address) - balanceBefore;
    emit ReceivedErc20(erc20_address, msg.sender, actualReceived, principal);
}
```

Apply the same pattern to `CkDeposit.depositErc20()` in `DepositHelperWithSubaccount.sol`. Since the contracts are immutable on Ethereum, new versions must be deployed and the minter upgraded via NNS proposal to point to the new helper contract addresses.

---

### Proof of Concept

1. A fee-on-transfer ERC-20 token `FeeToken` with a 1% transfer fee is added as a supported ckERC20 token via NNS governance.
2. Attacker calls `CkErc20Deposit.deposit(feeTokenAddress, 1_000_000, encodedPrincipal)` on the deployed helper contract at `0xb44b5e756a894775fc32eddf3314bb1b1944dc34`.
3. `safeTransferFrom` transfers `1_000_000` tokens from attacker; minter's Ethereum address receives `990_000` (1% fee deducted).
4. Helper emits `ReceivedErc20(feeTokenAddress, attacker, 1_000_000, encodedPrincipal)`.
5. IC minter scrapes the log, reads `value = 1_000_000`, and calls `icrc1_transfer` on the ckFeeToken ledger to mint `1_000_000` ckFeeToken to the attacker. [3](#0-2) 

6. Attacker now holds `1_000_000` ckFeeToken but the minter's Ethereum address only holds `990_000` FeeToken.
7. Attacker calls `withdraw_erc20` to redeem `1_000_000` ckFeeToken → FeeToken. The minter burns `1_000_000` ckFeeToken and constructs an Ethereum transaction sending `1_000_000` FeeToken from its address — but it only holds `990_000`, causing the Ethereum transaction to revert.
8. Repeated deposits by multiple users accumulate the shortfall, eventually making all withdrawals for this token fail.

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

**File:** rs/ethereum/cketh/minter/src/state.rs (L742-756)
```rust
    pub fn erc20_add(&mut self, erc20_contract: Address, deposit: Erc20Value) {
        match self.balance_by_erc20_contract.get(&erc20_contract) {
            Some(previous_value) => {
                let new_value = previous_value.checked_add(deposit).unwrap_or_else(|| {
                    panic!("BUG: overflow when adding {deposit} to {previous_value}")
                });
                self.balance_by_erc20_contract
                    .insert(erc20_contract, new_value);
            }
            None => {
                self.balance_by_erc20_contract
                    .insert(erc20_contract, deposit);
            }
        }
    }
```
