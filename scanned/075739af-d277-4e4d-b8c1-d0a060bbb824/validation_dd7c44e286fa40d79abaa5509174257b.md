### Title
Excess ckETH Transaction Fee Not Reimbursed to User After Successful ckERC20 Withdrawal - (File: rs/ethereum/cketh/minter/src/state/transactions/mod.rs)

---

### Summary

When a user calls `withdraw_erc20` on the ckETH minter, they pre-pay a `max_transaction_fee` in ckETH to cover the Ethereum gas cost. The actual gas consumed on Ethereum is almost always less than the maximum estimated fee. For **ckETH withdrawals**, the unused fee portion is reimbursed to the user on failure. For **ckERC20 withdrawals**, the unused ckETH fee (`max_transaction_fee - actual_tx_fee`) is **never returned to the user** regardless of whether the transaction succeeds or fails — it is permanently retained by the minter as "unspent transaction fees."

---

### Finding Description

In `record_finalized_transaction` in `rs/ethereum/cketh/minter/src/state/transactions/mod.rs`, when a finalized transaction is processed:

- For `WithdrawalRequest::CkEth`: on **failure**, the remaining `withdrawal_amount - effective_fee` is reimbursed to the user.
- For `WithdrawalRequest::CkErc20`: on **failure**, only the `withdrawal_amount` (the ckERC20 tokens) is reimbursed. The ckETH `max_transaction_fee` is **never reimbursed** regardless of success or failure.

The documentation in `rs/ethereum/cketh/docs/ckerc20.adoc` explicitly acknowledges this: *"Overcharged transaction fees are not reimbursed."*

The `update_balance_upon_withdrawal` function in `rs/ethereum/cketh/minter/src/state.rs` tracks the `unspent_tx_fee` for ckERC20 withdrawals as `req.max_transaction_fee - actual_tx_fee`, and adds it to `total_unspent_tx_fees` — confirming the excess is tracked but silently absorbed by the minter rather than returned.

The flow is:
1. User approves minter to burn `erc20_tx_fee` ckETH (estimated max gas cost).
2. Minter burns exactly `erc20_tx_fee` ckETH from user.
3. Ethereum transaction executes using only `actual_tx_fee < erc20_tx_fee`.
4. The difference `erc20_tx_fee - actual_tx_fee` is **never minted back** to the user.

This is structurally identical to the Sense Finance bug: tokens are transferred in for an operation, the operation does not consume all of them, and the excess is left stranded in the contract/minter rather than returned.

---

### Impact Explanation

Every successful ckERC20 withdrawal results in a permanent, silent loss of ckETH for the user equal to `max_transaction_fee - actual_tx_fee`. Given that the fee estimate includes a safety margin (at least 10% above current gas price to allow resubmissions), this excess is structurally non-zero on every withdrawal. The ckETH is burned from the user but the corresponding ETH value is retained by the minter's Ethereum balance as "unspent fees" — a ledger conservation violation where user funds are permanently lost.

**Impact**: Ledger conservation bug / chain-fusion mint/burn accounting bug. Every ckERC20 withdrawal permanently destroys user ckETH in excess of the actual Ethereum transaction cost.

---

### Likelihood Explanation

This affects **every** `withdraw_erc20` call that succeeds. The fee estimate is intentionally conservative (includes a 10% safety margin for resubmissions), so `actual_tx_fee < max_transaction_fee` is the normal case. Any unprivileged user calling `withdraw_erc20` triggers this path. The entry point is the public `withdraw_erc20` update method on the ckETH minter canister, callable by any non-anonymous principal.

---

### Recommendation

After a ckERC20 withdrawal transaction is finalized (success or failure), compute `unspent_fee = max_transaction_fee - actual_tx_fee` and, if positive, schedule a reimbursement of that amount in ckETH to the user (analogous to how ckETH withdrawals reimburse the unused fee on failure). This mirrors the fix applied in the Sense Finance audit: return excess tokens to the receiver at the end of the function.

---

### Proof of Concept

**Root cause — `record_finalized_transaction`:** [1](#0-0) 

For `CkErc20`, the block at line 733–745 only reimburses `withdrawal_amount` (the ckERC20 tokens) on failure. There is no branch that reimburses the unused ckETH `max_transaction_fee - actual_tx_fee` in either the success or failure case.

**Contrast with ckETH withdrawal reimbursement (failure path):** [2](#0-1) 

For `CkEth`, the reimbursed amount is `finalized_tx.transaction_amount()` which equals `withdrawal_amount - effective_fee` — i.e., the unused portion is returned.

**The unspent fee is tracked but never returned:** [3](#0-2) 

Line 360 shows `charged_tx_fee = req.max_transaction_fee` for ckERC20. Line 362 computes `unspent_tx_fee`. Line 375 adds it to `total_unspent_tx_fees` — it is accounted for in metrics but never minted back to the user.

**Test explicitly confirms no reimbursement on success:** [4](#0-3) 

The test `should_not_reimburse_unused_transaction_fee_when_ckerc20_withdrawal_successful` at line 1575 asserts `reimbursement_requests` is empty even when `gas_used = 40_000` out of `65_000` gas limit — confirming the excess fee is silently lost.

**Documentation acknowledges the behavior:** [5](#0-4) 

*"Overcharged transaction fees are not reimbursed."* — This is a documented design choice, but it constitutes a ledger conservation bug where user funds (ckETH) are permanently destroyed beyond the actual cost of the operation, analogous to the Sense Finance M-5 finding.

### Citations

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

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L275-275)
```text
. The minter retrieves the receipt of the finalized transaction (as done currently by the ckETH minter) and will reimburse the ckERC20 tokens in case the transaction failed. Overcharged transaction fees are not reimbursed.
```
