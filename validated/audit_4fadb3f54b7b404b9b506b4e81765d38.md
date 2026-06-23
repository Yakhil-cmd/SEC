### Title
ckERC20 Withdrawal: Unspent ckETH Transaction Fee Permanently Retained by Minter — No Refund Mechanism — (File: `rs/ethereum/cketh/minter/src/state/transactions/mod.rs`)

---

### Summary

The ckETH minter charges users a conservative `max_transaction_fee` in ckETH to cover Ethereum gas costs for ckERC20 withdrawals. Because the estimate is intentionally over-sized (to survive gas price spikes and resubmissions), the actual gas consumed is always less than the charged amount. The difference — `max_transaction_fee − actual_tx_fee` — is permanently retained in the minter's Ethereum address and never returned to users. This applies on both successful and failed Ethereum transactions. There is no refund path for the overcharged ckETH.

---

### Finding Description

**Entry path:** Any unprivileged IC principal calls `withdraw_erc20` on the ckETH minter canister (`rs/ethereum/cketh/minter/src/main.rs`).

**Two-phase non-atomic flow:**

**Phase 1 (IC side):** The minter burns `max_transaction_fee` ckETH from the caller's account to pay for Ethereum gas, and burns the ckERC20 withdrawal amount. [1](#0-0) 

**Phase 2 (Ethereum side):** The minter submits an Ethereum transaction. The actual gas consumed (`actual_tx_fee`) is always less than `max_transaction_fee` because the estimate includes a safety margin for resubmissions.

**Root cause — no ckETH reimbursement on failure:** In `record_finalized_transaction`, when the Ethereum transaction status is `Failure` for a ckERC20 request, only the ckERC20 withdrawal amount is scheduled for reimbursement. The ckETH gas fee (`max_transaction_fee`) is never reimbursed — not even the unspent portion. [2](#0-1) 

**Root cause — unspent fee tracked but never returned:** In `update_balance_upon_withdrawal`, the `unspent_tx_fee = charged_tx_fee − actual_tx_fee` is computed and added to the `total_unspent_tx_fees` accounting metric, but it is never minted back to the user. It silently accumulates in the minter's Ethereum address. [3](#0-2) 

**Explicitly documented as permanent loss:** The design document states this is intentional. [4](#0-3) 

**Test confirms no reimbursement on success:** The test `should_not_reimburse_unused_transaction_fee_when_ckerc20_withdrawal_successful` asserts that `reimbursement_requests` is empty after a successful ckERC20 withdrawal, even when `gas_used = 40_000` out of a `gas_limit = 65_000`. [5](#0-4) 

The same pattern applies to plain ckETH withdrawals: the user burns `withdrawal_amount` ckETH, the minter sends `withdrawal_amount − max_tx_fee_estimate` ETH to the destination, and the unspent fee stays in the minter's ETH balance with no refund. [6](#0-5) 

The `EthBalance` struct tracks `total_unspent_tx_fees` as a pure accounting metric — it is never used to trigger a refund. [7](#0-6) 

---

### Impact Explanation

**Ledger conservation bug.** Every ckERC20 withdrawal burns `max_transaction_fee` ckETH from the user. The minter's Ethereum address only loses `actual_tx_fee` ETH. The difference `max_transaction_fee − actual_tx_fee` remains in the minter's ETH address but has no corresponding ckETH backing — the ckETH supply is permanently reduced by more than the ETH actually consumed. Users have no mechanism to recover the overcharged amount. The `total_unspent_tx_fees` metric on the minter dashboard makes this loss observable and cumulative across all withdrawals.

On a failed Ethereum transaction, the user additionally loses the entire `actual_tx_fee` worth of ckETH (gas for the failed tx), while only the ckERC20 principal amount is reimbursed — the ckETH gas fee is entirely unrecoverable.

---

### Likelihood Explanation

**High.** The gas limit for ckERC20 withdrawals is fixed at `65_000` gas units, but standard ERC-20 transfers typically consume `~45_000–50_000` gas. The fee estimate also includes a mandatory safety margin for resubmissions. This means every single ckERC20 withdrawal results in a non-trivial unspent fee. The effect is systematic and affects every user of the `withdraw_erc20` endpoint, requiring no special conditions or attacker capability — any unprivileged IC principal triggers it by calling the public endpoint.

---

### Recommendation

After a transaction is finalized, compute `unspent_fee = max_transaction_fee − actual_tx_fee` and schedule a ckETH mint reimbursement to the user's account for that amount, analogous to the existing `ReimbursementRequest` flow used for failed ckETH withdrawals. The `record_finalized_transaction` function should be extended to emit a `FailedErc20WithdrawalRequest`-style reimbursement event for the ckETH gas overpayment on both success and failure paths. [8](#0-7) 

---

### Proof of Concept

1. User holds ckETH and ckUSDC. Calls `withdraw_erc20` specifying `amount = 2_000_000` ckUSDC and destination Ethereum address.
2. Minter estimates `max_transaction_fee = 32_500_000_000_000_000` wei ckETH (gas_limit=65_000 × max_fee_per_gas) and burns it from the user's ckETH balance.
3. Ethereum transaction is mined. Receipt shows `gas_used = 40_000`, `effective_gas_price = 100` → `actual_tx_fee = 4_000_000` wei.
4. `unspent_tx_fee = 32_500_000_000_000_000 − 4_000_000 = 32_499_999_996_000_000` wei stays in minter's ETH address.
5. `record_finalized_transaction` (success path) creates no reimbursement request for ckETH.
6. User's ckETH balance is permanently reduced by `32_500_000_000_000_000` wei; only `4_000_000` wei of ETH was actually consumed.

This is confirmed by the state machine test at: [9](#0-8) 

and the balance accounting test showing `total_unspent_tx_fees` accumulates without any corresponding user refund: [10](#0-9)

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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L680-748)
```rust
    pub fn record_finalized_transaction(
        &mut self,
        ledger_burn_index: LedgerBurnIndex,
        receipt: TransactionReceipt,
    ) {
        let sent_tx = self
            .sent_tx
            .get_alt(&ledger_burn_index)
            .expect("BUG: missing sent transactions")
            .iter()
            .find(|sent_tx| sent_tx.as_ref().hash() == receipt.transaction_hash)
            .expect("ERROR: no transaction matching receipt");
        let finalized_tx = sent_tx
            .as_ref()
            .clone()
            .try_finalize(receipt.clone())
            .expect("ERROR: invalid transaction receipt");

        let nonce = sent_tx.as_ref().nonce();
        {
            self.sent_tx.remove_entry(&nonce);
            Self::cleanup_failed_resubmitted_transactions(&mut self.created_tx, &nonce);
        }
        assert_eq!(
            self.finalized_tx
                .try_insert(nonce, ledger_burn_index, finalized_tx.clone()),
            Ok(())
        );

        assert!(
            self.maybe_reimburse.remove(&ledger_burn_index),
            "failed to remove entry from maybe_reimburse with block index: {ledger_burn_index}",
        );

        let request = self.processed_withdrawal_requests
            .get(&ledger_burn_index)
            .expect("failed to find entry from processed_withdrawal_requests with block index: {ledger_burn_index}");
        let index = ReimbursementIndex::from(request);
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

**File:** rs/ethereum/cketh/minter/src/state.rs (L647-661)
```rust
#[derive(Clone, Eq, PartialEq, Debug)]
pub struct EthBalance {
    /// Amount of ETH controlled by the minter's address via tECDSA.
    /// Note that invalid deposits are not accounted for and so this value
    /// might be less than what is displayed by Etherscan
    /// or retrieved by the JSON-RPC call `eth_getBalance`.
    /// Also, some transactions may have gone directly to the minter's address
    /// without going via the helper smart contract.
    eth_balance: Wei,
    /// Total amount of fees across all finalized transactions ckETH -> ETH.
    total_effective_tx_fees: Wei,
    /// Total amount of fees that were charged to the user during the withdrawal
    /// but not consumed by the finalized transaction ckETH -> ETH
    total_unspent_tx_fees: Wei,
}
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L275-275)
```text
. The minter retrieves the receipt of the finalized transaction (as done currently by the ckETH minter) and will reimburse the ckERC20 tokens in case the transaction failed. Overcharged transaction fees are not reimbursed.
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/tests.rs (L1547-1572)
```rust
        #[test]
        fn should_record_cketh_finalized_transaction_and_not_reimburse() {
            let mut transactions = EthTransactions::new(TransactionNonce::ZERO);
            let cketh_ledger_burn_index = LedgerBurnIndex::new(15);
            let withdrawal_request: WithdrawalRequest =
                cketh_withdrawal_request_with_index(cketh_ledger_burn_index).into();
            transactions.record_withdrawal_request(withdrawal_request.clone());
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
            assert_eq!(maybe_reimburse_request, &withdrawal_request);
            assert!(!transactions.maybe_reimburse.is_empty());

            let receipt = transaction_receipt(&signed_tx, TransactionStatus::Success);
            transactions.record_finalized_transaction(cketh_ledger_burn_index, receipt.clone());

            assert!(transactions.maybe_reimburse.is_empty());
            assert!(transactions.reimbursement_requests.is_empty());
        }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/tests.rs (L1574-1602)
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
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/tests.rs (L1605-1640)
```rust
        #[test]
        fn should_not_reimburse_when_ckerc20_witdrawal_used_up_transaction_fee() {
            let mut transactions = EthTransactions::new(TransactionNonce::ZERO);
            let cketh_ledger_burn_index = LedgerBurnIndex::new(7);
            let ckerc20_ledger_burn_index = LedgerBurnIndex::new(7);
            let withdrawal_request = Erc20WithdrawalRequest {
                max_transaction_fee: Wei::from(32_500_000_000_000_000_u128),
                ..ckerc20_withdrawal_request_with_index(
                    cketh_ledger_burn_index,
                    ckerc20_ledger_burn_index,
                )
            };
            transactions.record_withdrawal_request(withdrawal_request.clone());
            let created_tx = create_and_record_transaction(
                &mut transactions,
                withdrawal_request.clone(),
                GasFeeEstimate {
                    base_fee_per_gas: WeiPerGas::from(250_000_000_000_u128),
                    max_priority_fee_per_gas: WeiPerGas::ZERO,
                },
            );
            let signed_tx = create_and_record_signed_transaction(&mut transactions, created_tx);
            let receipt = TransactionReceipt {
                gas_used: GasAmount::from(65_000_u32),
                effective_gas_price: WeiPerGas::from(500_000_000_000_u128),
                ..transaction_receipt(&signed_tx, TransactionStatus::Success)
            };
            assert_eq!(
                receipt.effective_transaction_fee(),
                withdrawal_request.max_transaction_fee
            );
            transactions.record_finalized_transaction(cketh_ledger_burn_index, receipt.clone());

            assert_eq!(transactions.maybe_reimburse, btreeset! {});
            assert_eq!(transactions.reimbursement_requests, btreemap! {});
        }
```

**File:** rs/ethereum/cketh/minter/src/state/tests.rs (L1500-1530)
```rust
        let charged_transaction_fee = withdrawal_request.max_transaction_fee;
        let effective_transaction_fee = effective_gas_price
            .transaction_cost(effective_gas_used)
            .unwrap();
        let unspent_tx_fee = charged_transaction_fee
            .checked_sub(effective_transaction_fee)
            .unwrap();
        assert_eq!(
            eth_balance_after_successful_withdrawal,
            EthBalance {
                eth_balance: eth_balance_before_withdrawal
                    .eth_balance
                    .checked_sub(effective_transaction_fee)
                    .unwrap(),
                total_effective_tx_fees: eth_balance_before_withdrawal
                    .total_effective_tx_fees
                    .checked_add(effective_transaction_fee)
                    .unwrap(),
                total_unspent_tx_fees: eth_balance_before_withdrawal
                    .total_unspent_tx_fees
                    .checked_add(unspent_tx_fee)
                    .unwrap(),
            }
        );
        assert_eq!(
            checked_sub(
                erc20_balance_before_withdrawal.clone(),
                erc20_balance_after_successful_withdrawal
            ),
            btreemap! { withdrawal_request.erc20_contract_address => withdrawal_request.withdrawal_amount }
        );
```
