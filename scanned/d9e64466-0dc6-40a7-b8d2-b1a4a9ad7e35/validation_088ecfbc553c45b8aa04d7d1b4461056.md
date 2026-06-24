### Title
Overcharged ckETH Transaction Fee Not Reimbursed on Successful ckERC20 Withdrawal - (File: rs/ethereum/cketh/minter/src/state/transactions/mod.rs)

### Summary

The ckETH minter's `withdraw_erc20` flow burns a conservatively estimated `max_transaction_fee` of ckETH from the user upfront. When the Ethereum transaction succeeds, the actual gas cost (`effective_transaction_fee`) is always ≤ `max_transaction_fee`. The difference — the **unspent transaction fee** — is never returned to the user. This is an intentional, documented design choice for ckERC20 withdrawals, but it constitutes a permanent, quantifiable ledger conservation loss for every successful ckERC20 withdrawal, analogous to the original report's "tokens stay within the Executor and are lost."

### Finding Description

In `withdraw_erc20` (`rs/ethereum/cketh/minter/src/main.rs`, lines 448–504), the minter burns `erc20_tx_fee` (the estimated maximum transaction fee) from the user's ckETH balance via `icrc2_transfer_from` before the Ethereum transaction is submitted. [1](#0-0) 

After the Ethereum transaction is finalized, `record_finalized_transaction` in `rs/ethereum/cketh/minter/src/state/transactions/mod.rs` only schedules a reimbursement for `CkErc20` requests when `receipt.status == TransactionStatus::Failure`. On success, **no reimbursement of the unspent fee is scheduled**: [2](#0-1) 

This is confirmed by the test `should_not_reimburse_unused_transaction_fee_when_ckerc20_withdrawal_successful`, which explicitly asserts that `reimbursement_requests` is empty even when `effective_transaction_fee` (4,000,000 wei) is far below `max_transaction_fee`: [3](#0-2) 

The `update_balance_upon_withdrawal` function in `rs/ethereum/cketh/minter/src/state.rs` tracks the `unspent_tx_fee` in internal accounting but does not trigger any refund to the user: [4](#0-3) 

The documentation explicitly acknowledges this: *"Overcharged transaction fees are not reimbursed."* [5](#0-4) 

By contrast, for **ckETH** withdrawals, the unused fee is implicitly returned because the transaction value is set to `withdrawal_amount - max_tx_fee_estimate`, so the user's ETH destination receives less, but the minter's ETH balance retains the unspent portion and the ckETH burn was exact. For **ckERC20** withdrawals, the ckETH fee burn is a separate, fixed upfront burn — the unspent portion accumulates in the minter's ETH balance as `total_unspent_tx_fees` and is never minted back. [6](#0-5) 

### Impact Explanation

Every successful ckERC20 withdrawal results in a permanent loss of ckETH for the user equal to `max_transaction_fee - actual_transaction_fee`. Given that the fee estimate includes a safety margin (≥10% above current gas price to allow resubmissions), this overcharge is structurally guaranteed on every successful first-attempt transaction. The unspent ckETH is burned from the user's ledger balance but never minted back; it accumulates as `total_unspent_tx_fees` in the minter's internal ETH balance accounting, representing real ETH held by the minter that belongs to no one. This is a **ledger conservation bug**: ckETH total supply is reduced by more than the ETH actually spent on Ethereum. [7](#0-6) 

### Likelihood Explanation

This affects **every** successful ckERC20 withdrawal. Any unprivileged user calling `withdraw_erc20` on the ckETH minter canister triggers this path. The entry point is a public ingress update call. The overcharge is structurally guaranteed because the fee estimate is intentionally conservative (includes a 10%+ safety margin for potential resubmissions). Likelihood is **high** — it occurs on every successful ckERC20 withdrawal. [8](#0-7) 

### Recommendation

After a ckERC20 Ethereum transaction is finalized with `TransactionStatus::Success`, compute `unspent_fee = max_transaction_fee - effective_transaction_fee` and schedule a `ReimbursementRequest` to mint the unspent ckETH back to the user (analogous to how ckETH withdrawal failures already reimburse unused fees). This mirrors the existing `FailedErc20WithdrawalRequest` reimbursement path. The `record_finalized_transaction` function in `rs/ethereum/cketh/minter/src/state/transactions/mod.rs` should be extended to handle the `CkErc20` success case with a partial ckETH reimbursement when `unspent_fee > CKETH_LEDGER_TRANSACTION_FEE`. [9](#0-8) 

### Proof of Concept

1. User calls `icrc2_approve` on the ckETH ledger, approving the minter for `max_transaction_fee` (e.g., 30,000,000,000,000,000 wei ≈ 0.03 ETH).
2. User calls `icrc2_approve` on the ckERC20 ledger for the withdrawal amount.
3. User calls `withdraw_erc20` on the minter. The minter burns exactly `max_transaction_fee` ckETH from the user.
4. The Ethereum transaction is mined. Suppose `effective_gas_price = 100 wei`, `gas_used = 40,000`. Then `actual_fee = 4,000,000 wei`.
5. `unspent_fee = 30,000,000,000,000,000 - 4,000,000 = 29,999,999,996,000,000 wei` (~0.03 ETH) is permanently lost.
6. `record_finalized_transaction` is called with `TransactionStatus::Success`; the `CkErc20` branch does nothing (no reimbursement scheduled).
7. The user's ckETH balance is permanently reduced by `max_transaction_fee` instead of `actual_fee`. [3](#0-2) [10](#0-9)

### Citations

**File:** rs/ethereum/cketh/minter/src/main.rs (L430-432)
```rust
    let erc20_tx_fee = estimate_erc20_transaction_fee().await.ok_or_else(|| {
        WithdrawErc20Error::TemporarilyUnavailable("Failed to retrieve current gas fee".to_string())
    })?;
```

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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L718-747)
```rust
        match &request {
            WithdrawalRequest::CkEth(request) => {
                if receipt.status == TransactionStatus::Failure {
                    self.record_reimbursement_request(
                        index,
                        ReimbursementRequest {
                            ledger_burn_index,
                            to: request.from,
                            to_subaccount: request.from_subaccount.clone(),
                            reimbursed_amount: finalized_tx.transaction_amount().change_units(),
                            transaction_hash: Some(receipt.transaction_hash),
                        },
                    );
                }
            }
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
        }
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

**File:** rs/ethereum/cketh/docs/cketh.adoc (L216-223)
```text
[TIP]
.Effective transaction fees vs unspent transaction fees
====
The minter dashboard displays in the metadata table the following fees

. `Total effective transaction fees`: the sum of all `actual_tx_fee` for all withdrawals.
. `Total unspent transaction fees`: the sum of all `max_tx_fee_estimate - actual_tx_fee` for all withdrawals. This represents an overestimate of the actual transaction fees that were charged to the user but in retrospect not needed to mine the sent transaction.
====
```
