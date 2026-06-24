### Title
Fee-on-Transfer ERC-20 Deposit Overmints ckERC20 Tokens - (File: rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol, rs/ethereum/cketh/minter/ERC20DepositHelper.sol, rs/ethereum/cketh/minter/src/deposit.rs)

### Summary
The ckETH/ckERC20 minter canister mints ckERC20 tokens based on the `amount` field emitted in the `ReceivedEthOrErc20` / `ReceivedErc20` Ethereum log event, not the amount actually received by the minter's Ethereum address. For fee-on-transfer ERC-20 tokens, the helper smart contract emits the pre-fee `amount` in the event, but the minter's address receives `amount - fee`. The IC minter then mints the full `amount` of ckERC20 to the depositor, creating unbacked ckERC20 tokens and breaking the 1:1 backing invariant.

### Finding Description

The deposit helper contracts (`CkDeposit.depositErc20` and `CkErc20Deposit.deposit`) call `safeTransferFrom(msg.sender, minterAddress, amount)` and then emit the event with the caller-supplied `amount` parameter:

```solidity
// DepositHelperWithSubaccount.sol lines 519-531
erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
emit ReceivedEthOrErc20(erc20Address, msg.sender, amount, principal, subaccount);
```

For a standard ERC-20, `safeTransferFrom` transfers exactly `amount` tokens to `minterAddress`. However, for fee-on-transfer ERC-20 tokens (e.g., USDT on some chains, PAXG, STA), the token contract deducts a fee during transfer, so `minterAddress` receives `amount - fee`, while the event still records `amount`.

The IC minter scrapes these logs and mints ckERC20 using `event.value()`, which reads directly from the log's `amount` field:

```rust
// rs/ethereum/cketh/minter/src/deposit.rs lines 73-81
let block_index = match client
    .transfer(TransferArg {
        ...
        amount: event.value(),  // taken directly from the log event
    })
    .await
```

`event.value()` returns the `value` field of `ReceivedErc20Event`, which is parsed from the Ethereum log's `amount` parameter — the pre-fee amount. There is no cross-check against the minter's actual on-chain ERC-20 balance before and after the deposit.

The same pattern exists in the older `ERC20DepositHelper.sol`:
```solidity
// ERC20DepositHelper.sol lines 499-502
erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);
emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
```

### Impact Explanation

**Ledger conservation bug / chain-fusion mint/burn/replay bug.** An unprivileged user depositing a fee-on-transfer ERC-20 token causes the minter to mint more ckERC20 than the ERC-20 backing held by the minter's Ethereum address. Over time, the total ckERC20 supply exceeds the actual ERC-20 balance held by the minter, breaking the 1:1 backing guarantee. A sophisticated attacker can:

1. Repeatedly deposit a fee-on-transfer ERC-20 token, each time receiving `amount` ckERC20 while the minter only holds `amount - fee`.
2. Accumulate excess ckERC20 tokens.
3. Withdraw the excess ckERC20 back to Ethereum, draining ERC-20 tokens deposited by other legitimate users.

This results in direct loss of funds for other ckERC20 holders whose withdrawals will fail once the minter's ERC-20 balance is exhausted.

### Likelihood Explanation

**Medium.** The ckERC20 system is designed to support arbitrary ERC-20 tokens added via NNS governance proposals. While currently supported tokens (e.g., USDC, USDT on Ethereum mainnet) do not charge transfer fees, the minter architecture does not enforce this constraint. Any future addition of a fee-on-transfer ERC-20 token via governance would immediately expose this vulnerability. An unprivileged user triggers it simply by calling `depositErc20` with a fee-on-transfer token — no special access is required.

### Recommendation

1. **Measure actual received amount on-chain**: Modify the helper smart contract's `depositErc20` to record the minter's ERC-20 balance before and after the `safeTransferFrom`, and emit the actual received amount (`balanceAfter - balanceBefore`) in the event rather than the caller-supplied `amount`.

2. **Alternatively, document and enforce an allowlist**: Explicitly document that fee-on-transfer ERC-20 tokens are not supported, and add an on-chain check in the helper contract or a governance-enforced validation step that rejects such tokens before they are added to the supported list.

### Proof of Concept

**Root cause chain:**

1. User calls `depositErc20(feeTokenAddress, 1000, principal, subaccount)` on `CkDeposit`.
2. `safeTransferFrom(user, minterAddress, 1000)` executes; fee-on-transfer token deducts 10 tokens; minter receives 990.
3. Event emitted: `ReceivedEthOrErc20(feeTokenAddress, user, 1000, principal, subaccount)` — records 1000, not 990.
4. IC minter scrapes the log, parses `value = 1000` into `ReceivedErc20Event.value`.
5. `mint()` in `rs/ethereum/cketh/minter/src/deposit.rs` calls `client.transfer(TransferArg { amount: event.value(), ... })` — mints 1000 ckERC20.
6. Minter's Ethereum address holds only 990 ERC-20 tokens; 10 ckERC20 are unbacked.

**Key file references:**

- Helper contract emitting pre-fee amount: [1](#0-0) 
- Older helper contract with same pattern: [2](#0-1) 
- Minter minting directly from log event value without balance verification: [3](#0-2) 
- `event.value()` returning the raw log-parsed amount: [4](#0-3) 
- `ReceivedErc20Event.value` field sourced from Ethereum log: [5](#0-4)

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

**File:** rs/ethereum/cketh/minter/src/eth_logs/mod.rs (L205-210)
```rust
    pub fn value(&self) -> candid::Nat {
        match self {
            ReceivedEvent::Eth(evt) => evt.value.into(),
            ReceivedEvent::Erc20(evt) => evt.value.into(),
        }
    }
```
