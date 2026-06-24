### Title
Overcharged ckETH Transaction Fees Not Reimbursed on Successful ckERC20 Withdrawals - (`rs/ethereum/cketh/minter/src/state/transactions/mod.rs`)

---

### Summary

When a user performs a ckERC20 withdrawal, the ckETH minter burns a `max_transaction_fee` amount of ckETH upfront to cover Ethereum gas costs. The actual on-chain gas cost (`effective_transaction_fee`) is almost always less than `max_transaction_fee`. On a **successful** ckERC20 withdrawal, the minter intentionally does not reimburse the difference (`max_transaction_fee - effective_transaction_fee`) to the user. This is by design and is documented as such. However, this unspent fee is also not burned or accounted for as protocol revenue — it simply remains in the minter's Ethereum address as unspent ETH. A user who understands this can deliberately overpay the `max_transaction_fee` (by approving a large ckETH amount) and then trigger a successful withdrawal, causing the minter to permanently retain excess ckETH that was burned from the user but never actually spent on-chain. The analog to the original report is: the ckERC20 withdrawal path treats the overcharged fee as "consumed" without actually charging it to the Ethereum network, and never returns it to the user on success — creating a systematic, unrecoverable fee leak from users to the minter's ETH balance.

---

### Finding Description

In `rs/ethereum/cketh/minter/src/state/transactions/mod.rs`, the `record_finalized_transaction` function handles the post-finalization accounting for both ckETH and ckERC20 withdrawals:

```rust
WithdrawalRequest::CkErc20(request) => {
    if receipt.status == TransactionStatus::Failure {
        self.record_reimbursement_request(
            index,
            ReimbursementRequest {
                ledger_burn_index: request.ckerc20_ledger_burn_index,
                reimbursed_amount: request.withdrawal_amount.change_units(),
                ...
            },
        );
    }
}
``` [1](#0-0) 

On **success**, no reimbursement of any kind is issued for the ckERC20 path. The `max_transaction_fee` (burned upfront in ckETH) is never partially returned even when `effective_transaction_fee < max_transaction_fee`. This is confirmed by the test `should_not_reimburse_unused_transaction_fee_when_ckerc20_withdrawal_successful`, which explicitly asserts that `reimbursement_requests` is empty after a successful withdrawal where only 4,000,000 wei of gas was used out of a much larger `max_transaction_fee`. [2](#0-1) 

The contrast with ckETH withdrawals is stark: for ckETH, on **failure**, the user is reimbursed `withdrawal_amount - effective_fee_paid` (i.e., the unspent portion is returned). [3](#0-2) 

The `update_balance_upon_withdrawal` function in `rs/ethereum/cketh/minter/src/state.rs` confirms the accounting: for ckERC20, `charged_tx_fee = req.max_transaction_fee` (the full upfront burn), and `unspent_tx_fee = charged_tx_fee - actual_tx_fee` is tracked in `total_unspent_tx_fees` but never returned to the user. [4](#0-3) 

The documentation explicitly acknowledges this: "Overcharged transaction fees are not reimbursed." [5](#0-4) 

The `withdraw_erc20` endpoint burns the full `erc20_tx_fee` estimate from the user's ckETH account before the Ethereum transaction is even created: [6](#0-5) 

---

### Impact Explanation

Every successful ckERC20 withdrawal results in the user losing `max_transaction_fee - effective_transaction_fee` in ckETH permanently. This amount is tracked as `total_unspent_tx_fees` in the minter's internal accounting but is never returned to the user and is not burned as protocol revenue — it remains as surplus ETH in the minter's Ethereum address. The minter's ckETH supply becomes undercollateralized relative to its ETH holdings by exactly this surplus. Users who make many ckERC20 withdrawals during periods of high gas fee estimates (where `max_transaction_fee` is set conservatively high) lose disproportionate amounts of ckETH. The impact is a **ledger conservation bug**: the total ckETH burned exceeds the ETH actually spent on-chain, with the difference permanently locked in the minter's ETH address and unrecoverable by users.

---

### Likelihood Explanation

This affects **every** successful ckERC20 withdrawal. The `gas_limit` for ckERC20 withdrawals is fixed at `65_000`, but standard ERC-20 `transfer` calls typically use 30,000–45,000 gas, meaning the unspent fee is structurally non-zero on virtually every transaction. The fee estimate also includes a safety margin for resubmissions (at least 10% bumps), so `max_transaction_fee` is always set above the expected actual cost. Any user calling `withdraw_erc20` is affected. [7](#0-6) 

---

### Recommendation

On successful ckERC20 withdrawal finalization, compute `unspent_fee = max_transaction_fee - effective_transaction_fee` and issue a ckETH reimbursement to the user for this amount (minus the ledger transfer fee), analogous to how ckETH withdrawals handle the unused fee on failure. Alternatively, burn the unspent ckETH on the ledger to maintain the 1:1 ETH/ckETH backing invariant. The `update_balance_upon_withdrawal` already computes `unspent_tx_fee` correctly — it just needs to trigger a reimbursement request for ckERC20 success cases. [8](#0-7) 

---

### Proof of Concept

1. User calls `icrc2_approve` on the ckETH ledger, approving the minter for a large `max_transaction_fee` (e.g., `32_500_000_000_000_000` wei ≈ 0.0325 ETH).
2. User calls `icrc2_approve` on the ckERC20 ledger for the withdrawal amount.
3. User calls `withdraw_erc20` on the minter. The minter burns the full `max_transaction_fee` from the user's ckETH account.
4. The minter submits an Ethereum ERC-20 transfer transaction. The actual gas used is, say, 40,000 gas at an effective price of 100 wei/gas = `4,000,000` wei actual fee.
5. The transaction succeeds. `record_finalized_transaction` is called with `TransactionStatus::Success`.
6. The `CkErc20` branch only triggers reimbursement on `Failure` — on `Success`, nothing is returned.
7. The user has permanently lost `32_500_000_000_000_000 - 4_000_000 ≈ 32.496 × 10^12` wei in ckETH that was burned but not spent on-chain.

This is confirmed by the existing test `should_not_reimburse_unused_transaction_fee_when_ckerc20_withdrawal_successful` which asserts `reimbursement_requests` is empty even when `effective_transaction_fee = 4_000_000` wei and `max_transaction_fee` is much larger. [9](#0-8)

### Citations

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

**File:** rs/ethereum/cketh/minter/src/state/transactions/tests.rs (L1689-1737)
```rust
        #[test]
        fn should_record_finalized_transaction_and_reimburse_unused_tx_fee_when_cketh_withdrawal_fails()
         {
            let mut transactions = EthTransactions::new(TransactionNonce::ZERO);
            let withdrawal_request = cketh_withdrawal_request_with_index(LedgerBurnIndex::new(15));
            transactions.record_withdrawal_request(withdrawal_request.clone());
            let cketh_ledger_burn_index = withdrawal_request.ledger_burn_index;
            let created_tx = create_and_record_transaction(
                &mut transactions,
                withdrawal_request.clone(),
                gas_fee_estimate(),
            );
            let signed_tx = create_and_record_signed_transaction(&mut transactions, created_tx);
            let maybe_reimburse_request = transactions
                .maybe_reimburse_requests_iter()
                .find(|r| r.cketh_ledger_burn_index() == cketh_ledger_burn_index)
                .expect("maybe reimburse request not found");
            assert_eq!(maybe_reimburse_request, &withdrawal_request.clone().into());

            let receipt = transaction_receipt(&signed_tx, TransactionStatus::Failure);
            transactions.record_finalized_transaction(cketh_ledger_burn_index, receipt.clone());

            let finalized_transaction = transactions
                .get_finalized_transaction(&cketh_ledger_burn_index)
                .expect("finalized tx not found");

            assert!(transactions.maybe_reimburse.is_empty());
            let cketh_reimbursement_index = ReimbursementIndex::CkEth {
                ledger_burn_index: cketh_ledger_burn_index,
            };
            let reimbursement_request = transactions
                .reimbursement_requests
                .get(&cketh_reimbursement_index)
                .expect("reimbursement request not found");
            let effective_fee_paid = finalized_transaction.effective_transaction_fee();
            assert_eq!(
                reimbursement_request,
                &ReimbursementRequest {
                    transaction_hash: Some(receipt.transaction_hash),
                    ledger_burn_index: cketh_ledger_burn_index,
                    to: withdrawal_request.from,
                    to_subaccount: withdrawal_request.from_subaccount,
                    reimbursed_amount: withdrawal_request
                        .withdrawal_amount
                        .checked_sub(effective_fee_paid)
                        .unwrap()
                        .change_units()
                }
            );
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

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L270-271)
```text
. The ckETH minter attempts to estimate the current transaction fee and tries to burn the necessary amount of ckETH to pay for the transaction. The `gas_limit` for ckERC20 withdrawals is currently fixed to `65_000` and should be sufficient for standard ERC-20 contracts. This estimate must include some safety margin to ensure that the minter can resubmit the transaction if necessary, which requires an increase of at least 10% in the max priority fee per gas. If the burn fails (e.g., insufficient funds), the withdrawal request will be rejected. If the burn succeeds, the burn transaction index is used as the request identifier.
. The minter attempts to burn the specified token amount from the user account on the ckERC20 ledger. If the burn succeeds, the minter schedules a withdrawal task. If the burn fails (e.g., insufficient funds), the minter schedules the reimbursement of the burnt ckETH amount from the previous step minus some (small) penalty fee.
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L275-275)
```text
. The minter retrieves the receipt of the finalized transaction (as done currently by the ckETH minter) and will reimburse the ckERC20 tokens in case the transaction failed. Overcharged transaction fees are not reimbursed.
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L448-460)
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
    {
        Ok(cketh_ledger_burn_index) => {
```
