### Title
Fee-on-Transfer ERC-20 Tokens Break ckERC20 1:1 Peg — (`rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol`, `rs/ethereum/cketh/minter/src/deposit.rs`)

---

### Summary

The ckETH minter's ERC-20 deposit flow unconditionally trusts the `amount` field emitted in the `ReceivedEthOrErc20` / `ReceivedErc20` event log to determine how many ckERC20 tokens to mint. Both helper smart contracts emit the caller-supplied `amount` rather than the actual tokens received by the minter address. If any supported ERC-20 token activates a fee-on-transfer mechanism, the minter will mint more ckERC20 than it holds in ERC-20 collateral, permanently breaking the 1:1 peg and making the system insolvent.

---

### Finding Description

**Step 1 — Helper contract emits the requested amount, not the received amount.**

Both deposit helper contracts perform `safeTransferFrom(msg.sender, minterAddress, amount)` and then immediately emit the event with the same `amount` parameter — without checking the minter's actual post-transfer balance:

`rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol` lines 519–531:
```solidity
erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
emit ReceivedEthOrErc20(erc20Address, msg.sender, amount, principal, subaccount);
```

`rs/ethereum/cketh/minter/ERC20DepositHelper.sol` lines 500–502:
```solidity
erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);
emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
```

If the ERC-20 token deducts a fee during `transferFrom`, the minter address receives `amount - fee`, but the event records `amount`.

**Step 2 — The IC minter mints exactly `event.value()` without any balance reconciliation.**

In `rs/ethereum/cketh/minter/src/deposit.rs` lines 73–81, the `mint()` function issues an ICRC-1 transfer for `event.value()` — the raw value parsed from the Ethereum log:

```rust
let block_index = match client
    .transfer(TransferArg {
        amount: event.value(),   // ← taken directly from the log event
        ...
    })
    .await
```

**Step 3 — The minter's internal ERC-20 balance accounting is also inflated.**

`rs/ethereum/cketh/minter/src/state.rs` lines 332–338, `update_balance_upon_deposit` adds `event.value` to `erc20_balances`:

```rust
ReceivedEvent::Erc20(event) => self
    .erc20_balances
    .erc20_add(event.erc20_contract_address, event.value),
```

There is no balance-before/balance-after check at any point in the deposit pipeline.

**Step 4 — USDT is an explicitly supported token with dormant fee-on-transfer logic.**

The supported token list in `rs/ethereum/cketh/docs/ckerc20.adoc` lines 37–38 includes USDT (`0xdAC17F958D2ee523a2206206994597C13D831ec7`). USDT's Ethereum contract contains fee-on-transfer logic that is currently set to 0 but can be activated unilaterally by Tether at any time.

---

### Impact Explanation

If any supported ERC-20 token activates fee-on-transfer:

- The minter mints `N` ckERC20 tokens but only holds `N - fee` ERC-20 tokens.
- The `erc20_balances` internal accounting diverges from the actual on-chain balance.
- The ckERC20 token is no longer 1:1 backed; the system becomes insolvent.
- Later withdrawers cannot redeem their ckERC20 for the underlying ERC-20 — funds are permanently lost for some users.
- The `erc20_sub` call in `update_balance_upon_withdrawal` (state.rs line 382) will eventually panic with an underflow when the internal balance is exhausted before all ckERC20 is redeemed, halting the minter.

---

### Likelihood Explanation

USDT is a supported token and its fee-on-transfer code path is already deployed on Ethereum mainnet (the fee is simply set to 0). Tether can activate it without any on-chain governance vote. The original audit report explicitly called out USDT as the canonical example of this risk. Any future NNS proposal adding a new fee-on-transfer ERC-20 token would also trigger this issue immediately upon activation.

---

### Recommendation

In both helper contracts, replace the hardcoded `amount` in the emitted event with the actual received amount, computed via a balance-before/balance-after pattern:

```solidity
uint256 balanceBefore = IERC20(erc20Address).balanceOf(minterAddress);
erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
uint256 actualReceived = IERC20(erc20Address).balanceOf(minterAddress) - balanceBefore;
emit ReceivedEthOrErc20(erc20Address, msg.sender, actualReceived, principal, subaccount);
```

This ensures the IC minter mints only the amount actually received, preserving the 1:1 peg regardless of the token's fee behavior.

---

### Proof of Concept

1. USDT activates its fee-on-transfer at 0.1% (10 bps).
2. A user calls `depositErc20(USDT_ADDRESS, 1_000_000, principal, subaccount)` on the `CkDeposit` helper contract.
3. The helper calls `safeTransferFrom(user, minterAddress, 1_000_000)`. The USDT contract deducts 1_000 as fee; the minter receives **999_000** USDT.
4. The helper emits `ReceivedEthOrErc20(USDT, user, 1_000_000, principal, subaccount)` — recording the full `1_000_000`.
5. The IC minter scrapes the finalized Ethereum log via `scrape_logs()` → `register_deposit_events()` → `mint()`.
6. `mint()` calls `client.transfer(TransferArg { amount: event.value() /* = 1_000_000 */ })`, minting **1_000_000 ckUSDT** to the user.
7. `update_balance_upon_deposit` records `erc20_balances[USDT] += 1_000_000`, but the minter only holds 999_000 USDT on Ethereum.
8. After 1_000 such deposits, the minter has minted 1_000_000_000 ckUSDT but holds only 999_000_000 USDT — a shortfall of 1_000_000 USDT.
9. The last ~1_000 users to attempt ckUSDT → USDT withdrawal will find insufficient collateral; the minter's `erc20_sub` will eventually panic on underflow, halting all ERC-20 withdrawals. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol (L519-531)
```text
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

**File:** rs/ethereum/cketh/minter/src/state.rs (L332-339)
```rust
    fn update_balance_upon_deposit(&mut self, event: &ReceivedEvent) {
        match event {
            ReceivedEvent::Eth(event) => self.eth_balance.eth_balance_add(event.value),
            ReceivedEvent::Erc20(event) => self
                .erc20_balances
                .erc20_add(event.erc20_contract_address, event.value),
        };
    }
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L758-770)
```rust
    pub fn erc20_sub(&mut self, erc20_contract: Address, withdrawal_amount: Erc20Value) {
        let previous_value = self
            .balance_by_erc20_contract
            .get(&erc20_contract)
            .expect("BUG: Cannot subtract from a missing ERC-20 balance");
        let new_value = previous_value
            .checked_sub(withdrawal_amount)
            .unwrap_or_else(|| {
                panic!("BUG: underflow when subtracting {withdrawal_amount} from {previous_value}")
            });
        self.balance_by_erc20_contract
            .insert(erc20_contract, new_value);
    }
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L37-38)
```text
|USDT
|https://etherscan.io/token/0xdAC17F958D2ee523a2206206994597C13D831ec7[0xdAC17F958D2ee523a2206206994597C13D831ec7]
```
