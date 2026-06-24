### Title
ckERC20 Minter Mints Based on Event-Logged Amount Rather Than Actual Received Tokens, Breaking 1:1 Backing Invariant for Fee-on-Transfer ERC20 Tokens — (`rs/ethereum/cketh/minter/ERC20DepositHelper.sol`)

---

### Summary

The ckETH minter's ERC20 deposit helper contract emits the deposit event with the caller-supplied `amount` parameter rather than the actual number of tokens received by the minter's Ethereum address. The IC minter canister then mints ckERC20 tokens 1:1 against this event-logged value. For fee-on-transfer ERC20 tokens (where the recipient receives less than the transferred amount), the minter will mint more ckERC20 than it holds in ERC20 backing, permanently breaking the 1:1 conservation invariant and causing subsequent withdrawal transactions to fail on Ethereum.

---

### Finding Description

**Root cause — helper contract emits requested amount, not received amount:**

The `CkErc20Deposit.deposit()` function in `rs/ethereum/cketh/minter/ERC20DepositHelper.sol` calls `safeTransferFrom` and then immediately emits the event with the caller-supplied `amount`:

```solidity
function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
    IERC20 erc20Token = IERC20(erc20_address);
    erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);
    emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
}
``` [1](#0-0) 

For a fee-on-transfer ERC20 token, `safeTransferFrom` succeeds but the minter's address receives `amount - fee`, not `amount`. The emitted event still records `amount`.

**Minter mints based on the event value:**

The minter scrapes this log, parses the `value` field from the event data, and mints exactly `event.value()` ckERC20 tokens to the depositor:

```rust
let block_index = match client
    .transfer(TransferArg {
        ...
        amount: event.value(),  // taken directly from the log event
    })
    .await
``` [2](#0-1) 

**Internal balance tracking is also inflated:**

When the deposit event is accepted, `update_balance_upon_deposit` adds the event's `value` to the minter's internal `erc20_balances`:

```rust
fn update_balance_upon_deposit(&mut self, event: &ReceivedEvent) {
    match event {
        ReceivedEvent::Erc20(event) => self
            .erc20_balances
            .erc20_add(event.erc20_contract_address, event.value),
    };
}
``` [3](#0-2) 

This means `erc20_balances` records `amount` but the minter's Ethereum address only holds `amount - fee`.

**Withdrawal encodes the full ckERC20 burn amount:**

When a user withdraws, the minter constructs an Ethereum ERC20 `transfer` call encoding the full `withdrawal_amount` (equal to the ckERC20 burned):

```rust
data: TransactionCallData::Erc20Transfer {
    to: request.destination,
    value: request.withdrawal_amount,
}.encode(),
``` [4](#0-3) 

If the minter's actual ERC20 balance is less than `withdrawal_amount` (because fee-on-transfer reduced the deposited amount), the ERC20 `transfer` call reverts on Ethereum. The minter has already burned the user's ckERC20 tokens on the IC ledger, and the reimbursement path only reimburses ckERC20 on a failed transaction receipt — but the ckETH gas fee is not fully refunded. [5](#0-4) 

---

### Impact Explanation

For any fee-on-transfer ERC20 token added as a supported ckERC20 token:

1. **Over-minting**: Every deposit mints `fee` more ckERC20 than the minter holds in ERC20 backing. The total ckERC20 supply exceeds the minter's actual ERC20 holdings.
2. **Withdrawal failures**: When cumulative withdrawals approach the actual (reduced) ERC20 balance, Ethereum `transfer` calls revert. Users who burned ckERC20 tokens on the IC will have their Ethereum transactions fail. They receive a ckERC20 reimbursement but lose the ckETH gas fee.
3. **Permanent fund lock**: The excess ckERC20 in circulation can never be redeemed because the backing ERC20 tokens do not exist at the minter's address.

---

### Likelihood Explanation

The NNS can add any ERC20 token as a supported ckERC20 token via governance proposal. Fee-on-transfer tokens (e.g., tokens with deflationary mechanics, certain DeFi tokens) are a well-known ERC20 variant. There is no on-chain or off-chain check in the minter or helper contract that rejects fee-on-transfer tokens. Any user can trigger the vulnerability simply by depositing a supported fee-on-transfer token through the normal deposit flow — no privileged access is required.

---

### Recommendation

The helper contract should measure the actual received amount by querying the minter's balance before and after the `safeTransferFrom`, and emit that delta as the event value:

```solidity
function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
    IERC20 erc20Token = IERC20(erc20_address);
    uint256 balanceBefore = erc20Token.balanceOf(cketh_minter_main_address);
    erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);
    uint256 actualReceived = erc20Token.balanceOf(cketh_minter_main_address) - balanceBefore;
    emit ReceivedErc20(erc20_address, msg.sender, actualReceived, principal);
}
```

Alternatively, the NNS governance process for adding new ckERC20 tokens should explicitly screen out fee-on-transfer tokens, and the minter documentation should state this restriction clearly.

---

### Proof of Concept

1. An NNS proposal adds a fee-on-transfer ERC20 token (e.g., 1% fee per transfer) as a supported ckERC20 token.
2. Alice calls `CkErc20Deposit.deposit(token, 1_000_000, alice_principal)`.
3. The helper contract calls `safeTransferFrom(alice, minter_address, 1_000_000)`. Due to the 1% fee, the minter receives `990_000` tokens.
4. The helper contract emits `ReceivedErc20(token, alice, 1_000_000, alice_principal)`.
5. The IC minter scrapes the log, reads `value = 1_000_000`, and mints `1_000_000` ckERC20 to Alice. [2](#0-1) 
6. Alice calls `withdraw_erc20` for `1_000_000` ckERC20. The minter burns `1_000_000` ckERC20 and constructs an Ethereum transaction calling `transfer(alice_eth_addr, 1_000_000)` on the ERC20 contract. [4](#0-3) 
7. The Ethereum transaction reverts because the minter only holds `990_000` tokens. Alice's ckERC20 is reimbursed but her ckETH gas fee is lost.
8. After many deposits, the total ckERC20 supply far exceeds the minter's actual ERC20 holdings. The last users to withdraw find their transactions permanently failing.

### Citations

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

**File:** rs/ethereum/cketh/minter/src/state.rs (L332-338)
```rust
    fn update_balance_upon_deposit(&mut self, event: &ReceivedEvent) {
        match event {
            ReceivedEvent::Eth(event) => self.eth_balance.eth_balance_add(event.value),
            ReceivedEvent::Erc20(event) => self
                .erc20_balances
                .erc20_add(event.erc20_contract_address, event.value),
        };
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L377-383)
```rust
        if receipt.status == TransactionStatus::Success && !tx.transaction_data().is_empty() {
            let TransactionCallData::Erc20Transfer { to: _, value } = TransactionCallData::decode(
                tx.transaction_data(),
            )
            .expect("BUG: failed to decode transaction data from transaction issued by minter");
            self.erc20_balances.erc20_sub(*tx.destination(), value);
        }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1177-1181)
```rust
                data: TransactionCallData::Erc20Transfer {
                    to: request.destination,
                    value: request.withdrawal_amount,
                }
                .encode(),
```
