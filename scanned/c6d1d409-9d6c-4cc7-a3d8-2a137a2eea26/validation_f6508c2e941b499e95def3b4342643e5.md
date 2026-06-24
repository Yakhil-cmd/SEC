### Title
Overcharged ckETH Transaction Fee Not Reimbursed to User on Successful ckERC20 Withdrawal - (File: rs/ethereum/cketh/minter/src/state/transactions/mod.rs)

### Summary
When a user performs a `withdraw_erc20` on the ckETH minter, they pre-pay a `max_transaction_fee` in ckETH to cover the Ethereum gas cost. The actual gas consumed on Ethereum is almost always less than the maximum estimated. For ckETH withdrawals, the unspent fee difference is implicitly returned to the user (the transaction sends `withdrawal_amount - max_fee_estimate`, so any unspent fee stays in the minter's ETH balance and is tracked as `total_unspent_tx_fees`). For ckERC20 withdrawals, the user burns exactly `max_transaction_fee` in ckETH upfront, but when the Ethereum transaction succeeds and uses less gas than the maximum, the overcharged ckETH (`max_transaction_fee - actual_tx_fee`) is **never minted back to the user**. This is an explicit, documented design decision — but it constitutes a ledger conservation violation: ckETH tokens are permanently destroyed without a corresponding ETH debit on Ethereum.

### Finding Description

In `withdraw_erc20` (`rs/ethereum/cketh/minter/src/main.rs`, lines 389–543), the minter burns exactly `erc20_tx_fee` (the estimated `max_transaction_fee`) from the user's ckETH account before the Ethereum transaction is sent. [1](#0-0) 

When the Ethereum transaction finalizes, `record_finalized_transaction` in `EthTransactions` only schedules a ckERC20 reimbursement if the transaction **failed** — it does nothing for the overcharged ckETH fee on success: [2](#0-1) 

In `update_balance_upon_withdrawal` (`rs/ethereum/cketh/minter/src/state.rs`), the minter correctly computes `unspent_tx_fee = max_transaction_fee - actual_tx_fee` and records it in `total_unspent_tx_fees`, but this amount is **never minted back** to the user: [3](#0-2) 

The documentation explicitly acknowledges this: *"Overcharged transaction fees are not reimbursed."* [4](#0-3) 

The test `should_not_reimburse_unused_transaction_fee_when_ckerc20_withdrawal_successful` confirms this is intentional behavior: [5](#0-4) 

The gas limit for ckERC20 withdrawals is fixed at `65_000`: [6](#0-5) 

Standard ERC-20 `transfer` calls typically consume 30,000–50,000 gas, meaning the overcharge is routinely 23%–54% of the burned ckETH fee.

### Impact Explanation

Every successful ckERC20 withdrawal permanently destroys ckETH tokens in excess of the actual Ethereum transaction fee paid. The minter's ETH balance on Ethereum retains the unspent gas (since the Ethereum network only charges `actual_gas_used * effective_gas_price`), but the corresponding ckETH was already burned and is never re-minted. This breaks the 1:1 backing invariant of ckETH: the minter holds more ETH than the total ckETH supply accounts for, but users cannot reclaim the difference. Over time, as ckERC20 withdrawals accumulate, the aggregate unspent fee grows, representing a permanent, irreversible loss of ckETH value for users who perform ckERC20 withdrawals.

### Likelihood Explanation

This affects **every** successful ckERC20 withdrawal. Any unprivileged user who calls `withdraw_erc20` on the ckETH minter canister (`sv3dd-oaaaa-aaaar-qacoa-cai`) triggers this path. The entry point is the public `withdraw_erc20` update call, reachable by any non-anonymous IC principal. The gas limit is fixed at 65,000 and standard ERC-20 transfers use 21,000–50,000 gas, so the overcharge is structurally guaranteed on every successful withdrawal.

### Recommendation

After the Ethereum transaction is finalized with `TransactionStatus::Success` for a `CkErc20` withdrawal request, compute `unspent_fee = max_transaction_fee - actual_tx_fee` and schedule a ckETH reimbursement mint for that amount to the user's `from` account (analogous to how ckETH withdrawals implicitly return the unspent fee via the transaction amount). This restores the ledger conservation property. The `update_balance_upon_withdrawal` function already computes `unspent_tx_fee` correctly; the missing step is triggering a `ReimbursementRequest` for the ckETH ledger when `unspent_tx_fee > 0` and the transaction succeeded. [7](#0-6) 

### Proof of Concept

1. User calls `withdraw_erc20` with a ckERC20 token. The minter burns `max_transaction_fee` (e.g., `30_000_000_000_000_000` wei ckETH) from the user's account.
2. The minter sends an Ethereum ERC-20 `transfer` transaction with `gas_limit = 65_000`.
3. The transaction succeeds on Ethereum using only `45_000` gas at `400 gwei` effective gas price → `actual_tx_fee = 18_000_000_000_000_000` wei.
4. `unspent_fee = 30_000_000_000_000_000 - 18_000_000_000_000_000 = 12_000_000_000_000_000` wei ckETH.
5. `record_finalized_transaction` for `CkErc20` with `TransactionStatus::Success` records **no reimbursement request**.
6. The `12_000_000_000_000_000` wei ckETH is permanently destroyed. The minter's Ethereum address retains the corresponding ETH, but no ckETH is ever minted back to the user.

This is confirmed by the unit test `should_not_reimburse_unused_transaction_fee_when_ckerc20_withdrawal_successful` which asserts `reimbursement_requests` is empty after a successful ckERC20 withdrawal even when `effective_transaction_fee < max_transaction_fee`. [5](#0-4)

### Citations

**File:** rs/ethereum/cketh/minter/src/main.rs (L448-458)
```rust
    match cketh_ledger
        .burn_from(
            cketh_account,
            erc20_tx_fee,
            BurnMemo::Erc20GasFee {
                ckerc20_token_symbol: ckerc20_token.ckerc20_token_symbol.clone(),
                ckerc20_withdrawal_amount,
                to_address: destination,
            },
        )
        .await
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L733-746)
```rust
            WithdrawalRequest::CkErc20(request) => {
                if receipt.status == TransactionStatus::Failure {
                    self.record_reimbursement_request(
                        index,
                        ReimbursementRequest {
                            ledger_burn_index: request.ckerc20_ledger_burn_index,
                            reimbursed_amount: request.withdrawal_amount.change_units(),
                            to: request.from,
                            to_subaccount: request.from_subaccount.clone(),
                            transaction_hash: Some(receipt.transaction_hash),
                        },
                    );
                }
            }
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L355-375)
```rust
        let charged_tx_fee = match withdrawal_request {
            WithdrawalRequest::CkEth(req) => req
                .withdrawal_amount
                .checked_sub(tx.transaction().amount)
                .expect("BUG: withdrawal amount MUST always be at least the transaction amount"),
            WithdrawalRequest::CkErc20(req) => req.max_transaction_fee,
        };
        let unspent_tx_fee = charged_tx_fee.checked_sub(tx_fee).expect(
            "BUG: charged transaction fee MUST always be at least the effective transaction fee",
        );
        let debited_amount = match receipt.status {
            TransactionStatus::Success => tx
                .transaction()
                .amount
                .checked_add(tx_fee)
                .expect("BUG: debited amount always fits into U256"),
            TransactionStatus::Failure => tx_fee,
        };
        self.eth_balance.eth_balance_sub(debited_amount);
        self.eth_balance.total_effective_tx_fees_add(tx_fee);
        self.eth_balance.total_unspent_tx_fees_add(unspent_tx_fee);
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L275-275)
```text
. The minter retrieves the receipt of the finalized transaction (as done currently by the ckETH minter) and will reimburse the ckERC20 tokens in case the transaction failed. Overcharged transaction fees are not reimbursed.
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/tests.rs (L1574-1603)
```rust
        #[test]
        fn should_not_reimburse_unused_transaction_fee_when_ckerc20_withdrawal_successful() {
            let mut transactions = EthTransactions::new(TransactionNonce::ZERO);
            let cketh_ledger_burn_index = LedgerBurnIndex::new(7);
            let ckerc20_ledger_burn_index = LedgerBurnIndex::new(7);
            let withdrawal_request = ckerc20_withdrawal_request_with_index(
                cketh_ledger_burn_index,
                ckerc20_ledger_burn_index,
            );
            transactions.record_withdrawal_request(withdrawal_request.clone());
            let created_tx = create_and_record_transaction(
                &mut transactions,
                withdrawal_request.clone(),
                gas_fee_estimate(),
            );
            let signed_tx = create_and_record_signed_transaction(&mut transactions, created_tx);
            let receipt = TransactionReceipt {
                gas_used: GasAmount::from(40_000_u32),
                effective_gas_price: WeiPerGas::from(100_u16),
                ..transaction_receipt(&signed_tx, TransactionStatus::Success)
            };
            assert_eq!(
                receipt.effective_transaction_fee(),
                Wei::from(4_000_000_u32)
            );
            transactions.record_finalized_transaction(cketh_ledger_burn_index, receipt.clone());

            assert_eq!(transactions.maybe_reimburse, btreeset! {});
            assert_eq!(transactions.reimbursement_requests, btreemap! {});
        }
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L43-44)
```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
```
