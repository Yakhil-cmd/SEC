### Title
ckERC20 Minter Mints Full Event Amount for Fee-on-Transfer / Rebasing ERC-20 Tokens, Causing Unbacked ckERC20 Supply - (File: rs/ethereum/cketh/minter/ERC20DepositHelper.sol, rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol, rs/ethereum/cketh/minter/src/deposit.rs)

---

### Summary

The ckETH minter's ERC-20 deposit flow unconditionally mints ckERC20 tokens equal to the `amount` field emitted in the `ReceivedErc20` / `ReceivedEthOrErc20` Ethereum log event. For fee-on-transfer ERC-20 tokens (e.g., USDT on some chains, SHIB, or any token with a transfer tax), the helper contract emits `amount` as the *requested* transfer amount, but the minter's Ethereum address actually receives `amount - fee`. The minter mints the full `amount` in ckERC20 on the IC ledger, creating unbacked ckERC20 tokens. When users later withdraw those ckERC20 tokens back to Ethereum, the minter attempts to send the full `amount` but only holds `amount - fee`, causing the withdrawal transaction to fail or drain reserves from other depositors.

---

### Finding Description

**Deposit helper contracts emit the requested `amount`, not the actually-received amount.**

In `ERC20DepositHelper.sol`:

```solidity
function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
    IERC20 erc20Token = IERC20(erc20_address);
    erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);
    emit ReceivedErc20(erc20_address, msg.sender, amount, principal);  // <-- emits `amount`, not actual received
}
```

In `DepositHelperWithSubaccount.sol` (`CkDeposit.depositErc20`):

```solidity
erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
emit ReceivedEthOrErc20(erc20Address, msg.sender, amount, principal, subaccount);  // <-- same issue
```

For a fee-on-transfer token, `safeTransferFrom` succeeds but the minter address receives `amount - fee`. The event still logs `amount`.

**The IC minter reads the event value and mints that exact amount.**

In `rs/ethereum/cketh/minter/src/deposit.rs`, the `mint()` function calls:

```rust
amount: event.value(),
```

where `event.value()` is the `value` field parsed directly from the Ethereum log — the `amount` parameter, not the actual balance change.

**The internal ERC-20 balance tracker is also inflated.**

In `rs/ethereum/cketh/minter/src/state.rs`, `update_balance_upon_deposit` adds `event.value` to `erc20_balances`:

```rust
ReceivedEvent::Erc20(event) => self
    .erc20_balances
    .erc20_add(event.erc20_contract_address, event.value),
```

This means the minter's internal accounting believes it holds more ERC-20 than it actually does on Ethereum.

**Withdrawal uses the stored (inflated) amount.**

When a user calls `withdraw_erc20`, the minter burns the ckERC20 and issues an Ethereum ERC-20 transfer for the full `withdrawal_amount`. If the minter's actual on-chain ERC-20 balance is less than the sum of all ckERC20 in circulation (due to accumulated fee-on-transfer discrepancies), the Ethereum transaction will revert or the minter will be unable to serve all withdrawal requests.

---

### Impact Explanation

**Unbacked ckERC20 supply / ledger conservation bug.** For every deposit of a fee-on-transfer ERC-20 token, the ckERC20 ledger total supply exceeds the actual ERC-20 balance held by the minter's Ethereum address. The discrepancy accumulates with each deposit. Eventually:

1. Withdrawal transactions for the last users to withdraw will fail because the minter's Ethereum address lacks sufficient ERC-20 balance.
2. The minter's internal `erc20_balances` counter diverges from reality, causing `erc20_balance_sub` to panic (underflow) when the minter tries to account for a withdrawal that exceeds the actual on-chain balance.
3. Excess ckERC20 tokens are permanently unbacked — they cannot be redeemed for real ERC-20 tokens.

This is a direct ledger conservation violation: `sum(ckERC20 supply) > actual ERC-20 held by minter`.

---

### Likelihood Explanation

The ckETH minter already supports ckSHIB (SHIB token), which historically has had fee-on-transfer mechanics, and ckUSDT (Tether), which has a configurable transfer fee that has been non-zero in the past. Any NNS proposal to add a fee-on-transfer ERC-20 token via the ledger suite orchestrator would trigger this bug for every deposit. The entry path is fully unprivileged: any Ethereum user can call `depositErc20` on the helper contract with a fee-on-transfer token. No special access is required.

---

### Recommendation

1. In the helper contracts, measure the actual balance change rather than trusting `amount`:
   ```solidity
   uint256 balanceBefore = erc20Token.balanceOf(minterAddress);
   erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
   uint256 actualReceived = erc20Token.balanceOf(minterAddress) - balanceBefore;
   emit ReceivedEthOrErc20(erc20Address, msg.sender, actualReceived, principal, subaccount);
   ```
2. Alternatively, the IC minter canister should verify the actual ERC-20 balance change on the minter address when processing deposit events, rather than trusting the event's `amount` field.
3. Document and enforce a policy that fee-on-transfer and rebasing ERC-20 tokens are not supported, and add a validation step in the NNS proposal flow for adding new ckERC20 tokens.

---

### Proof of Concept

1. A fee-on-transfer ERC-20 token `FOT` with a 1% transfer fee is added as a supported ckERC20 token via NNS proposal.
2. User calls `depositErc20(FOT_address, 1_000_000, principal)` on the helper contract.
3. The helper calls `FOT.safeTransferFrom(user, minter, 1_000_000)`. Due to the 1% fee, the minter receives `990_000` FOT. The event emits `amount = 1_000_000`.
4. The IC minter scrapes the log, reads `value = 1_000_000`, and mints `1_000_000` ckFOT to the user.
5. The minter's `erc20_balances` records `1_000_000` FOT held, but only `990_000` is actually on-chain.
6. After 100 such deposits, the minter believes it holds `100_000_000` FOT but actually holds `99_000_000`.
7. The 100th user to withdraw `1_000_000` ckFOT triggers an Ethereum ERC-20 transfer that fails because the minter's balance is exhausted, or earlier users' withdrawals drain the pool leaving the last user unable to redeem.

**Root cause files:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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
