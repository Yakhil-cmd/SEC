The key evidence is now clear. Let me trace the exact math.

**`transaction_receipt` test helper** (line 2906-2907):
```rust
effective_gas_price: signed_tx.transaction().max_fee_per_gas,
gas_used: signed_tx.transaction().gas_limit,
```

The test uses a synthetic receipt where `effective_gas_price == max_fee_per_gas` and `gas_used == gas_limit`, making `effective_fee_paid == max_transaction_fee`. This is why the test at line 1724-1736 passes — it's a degenerate case where the actual fee equals the maximum fee. In production, `effective_gas_price < max_fee_per_gas` almost always.

**The actual code path for failed ckETH withdrawals:**

- `create_transaction`: `tx.amount = withdrawal_amount - max_fee_per_gas * gas_limit` [1](#0-0) 
- On resubmission via `ReduceEthAmount`: `new_amount = withdrawal_amount - new_max_fee * gas_limit` (original `withdrawal_amount` is preserved in the strategy, but the tx `amount` field shrinks with each fee bump) [2](#0-1) 
- `FinalizedEip1559Transaction::transaction_amount()` returns `self.transaction.transaction().amount` — the EIP-1559 `amount` field, i.e. `withdrawal_amount - max_fee_N * gas_limit` [3](#0-2) 
- `record_finalized_transaction` sets `reimbursed_amount = finalized_tx.transaction_amount()` = `withdrawal_amount - max_fee_N * gas_limit` [4](#0-3) 

**The invariant that breaks:**

Correct reimbursement = `withdrawal_amount - actual_fee_paid` = `withdrawal_amount - effective_gas_price * gas_used`

Actual reimbursement = `withdrawal_amount - max_fee_N * gas_limit`

User loss = `max_fee_N * gas_limit - effective_gas_price * gas_used > 0` (always positive in practice, since EIP-1559 guarantees `effective_gas_price ≤ max_fee_per_gas` and failed txs often use less gas than the limit)

With N resubmissions, `max_fee_N` grows by at least 10% per round, so the loss compounds.

**The test gap:**

The test named `should_record_finalized_transaction_and_reimburse_unused_tx_fee_when_cketh_withdrawal_fails` asserts `withdrawal_amount - effective_fee_paid`, but only passes because the helper sets `effective_gas_price = max_fee_per_gas` and `gas_used = gas_limit`, making the two quantities equal. No test covers the resubmission-then-failure path. [5](#0-4) 

---

### Title
ckETH Failed-Withdrawal Reimbursement Uses `transaction_amount` Instead of `withdrawal_amount - actual_fee`, Causing User ckETH Loss — (`rs/ethereum/cketh/minter/src/state/transactions/mod.rs`)

### Summary
When a ckETH withdrawal fails on-chain (after zero or more resubmissions), `record_finalized_transaction` reimburses the user `finalized_tx.transaction_amount()` = `withdrawal_amount - max_fee_N * gas_limit`. The correct amount is `withdrawal_amount - effective_gas_price * gas_used`. The difference — the "unspent" portion of the maximum fee — is silently retained in the minter's ETH balance and never returned to the user.

### Finding Description
In `EthTransactions::record_finalized_transaction`:

```rust
// line 727
reimbursed_amount: finalized_tx.transaction_amount().change_units(),
```

`finalized_tx.transaction_amount()` is the EIP-1559 `amount` field of the signed transaction — the ETH value that *would have been* sent to the recipient. For a ckETH withdrawal this equals `withdrawal_amount - max_fee_per_gas * gas_limit` (initial creation) or `withdrawal_amount - max_fee_N * gas_limit` after N resubmissions via `ReduceEthAmount`.

The actual on-chain cost of a failed transaction is `effective_gas_price * gas_used` (from the receipt), which is always ≤ `max_fee_per_gas * gas_limit`. The gap is never refunded.

The `ReduceEthAmount` resubmission strategy (used for all ckETH withdrawals) stores the original `withdrawal_amount` and recomputes `tx.amount = withdrawal_amount - new_max_fee * gas_limit` on each bump. After N resubmissions the max fee is at least `(1.1)^N` times the original, so the user's loss grows with each resubmission round.

The unit test `should_record_finalized_transaction_and_reimburse_unused_tx_fee_when_cketh_withdrawal_fails` does not catch this because its synthetic receipt sets `effective_gas_price = max_fee_per_gas` and `gas_used = gas_limit`, making the two quantities accidentally equal.

### Impact Explanation
A user who burns `W` ckETH and whose withdrawal transaction fails after N resubmissions receives back `W - max_fee_N * gas_limit` ckETH. They should receive `W - effective_gas_price * gas_used`. The shortfall `max_fee_N * gas_limit - effective_gas_price * gas_used` is a permanent ckETH loss for the user. For a typical ETH withdrawal (gas_limit = 21,000) with a 2× fee spike and one resubmission, this can be on the order of tens of thousands of gwei (hundreds of USD at high gas prices). The minter's ETH balance gains the corresponding ETH, creating a supply/backing imbalance that benefits the minter pool at the expense of the individual withdrawer.

### Likelihood Explanation
Gas spikes causing resubmissions are routine on Ethereum mainnet. EVM transaction failures (e.g., out-of-gas, reverts) are also common. The combination is realistic and requires no privileged access — any user calling `withdraw_eth` is exposed. The loss is proportional to the fee spike magnitude and number of resubmissions.

### Recommendation
Replace line 727 with the correct formula:

```rust
reimbursed_amount: request.withdrawal_amount
    .checked_sub(finalized_tx.effective_transaction_fee())
    .unwrap_or(Wei::ZERO)
    .change_units(),
```

`finalized_tx.effective_transaction_fee()` returns `receipt.effective_gas_price * receipt.gas_used`, which is the true on-chain cost. Also update the unit test to use a receipt where `effective_gas_price < max_fee_per_gas` to actually exercise the unused-fee path.

### Proof of Concept
State-machine test (no privileged access needed):

1. Create a ckETH withdrawal request for `W = 100_000_000_000_000_000` wei.
2. Create initial transaction: `tx.amount = W - max_fee_0 * 21_000`.
3. Resubmit twice with 10% fee bumps: `max_fee_2 ≈ 1.21 * max_fee_0`.
4. Finalize with a `Failure` receipt where `effective_gas_price = max_fee_0` (realistic — the actual price is the original estimate, not the bumped one) and `gas_used = 21_000`.
5. Assert: `reimbursement_request.reimbursed_amount == W - max_fee_0 * 21_000` (correct).
6. Observe: actual value is `W - max_fee_2 * 21_000` (wrong — user loses `(max_fee_2 - max_fee_0) * 21_000` extra ckETH). [6](#0-5) [3](#0-2) [2](#0-1) [7](#0-6) [5](#0-4)

### Citations

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L718-731)
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
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1125-1142)
```rust
            let tx_amount = match request.withdrawal_amount.checked_sub(max_transaction_fee) {
                Some(tx_amount) => tx_amount,
                None => {
                    return Err(CreateTransactionError::InsufficientTransactionFee {
                        cketh_ledger_burn_index: request.ledger_burn_index,
                        allowed_max_transaction_fee: request.withdrawal_amount,
                        actual_max_transaction_fee: max_transaction_fee,
                    });
                }
            };
            Ok(Eip1559TransactionRequest {
                chain_id: ethereum_network.chain_id(),
                nonce,
                max_priority_fee_per_gas: transaction_price.max_priority_fee_per_gas,
                max_fee_per_gas: transaction_price.max_fee_per_gas,
                gas_limit: transaction_price.gas_limit,
                destination: request.destination,
                amount: tx_amount,
```

**File:** rs/ethereum/cketh/minter/src/tx.rs (L175-181)
```rust
        let new_amount = match self.resubmission {
            ResubmissionStrategy::ReduceEthAmount { withdrawal_amount } => {
                withdrawal_amount.checked_sub(new_tx_price.max_transaction_fee())
                    .expect("BUG: withdrawal_amount covers new transaction fee because it was checked before")
            }
            ResubmissionStrategy::GuaranteeEthAmount { .. } => transaction_request.amount,
        };
```

**File:** rs/ethereum/cketh/minter/src/tx.rs (L313-315)
```rust
    pub fn transaction_amount(&self) -> &Wei {
        &self.transaction.transaction().amount
    }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/tests.rs (L1689-1738)
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
        }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/tests.rs (L2895-2910)
```rust
fn transaction_receipt(
    signed_tx: &SignedEip1559TransactionRequest,
    status: TransactionStatus,
) -> TransactionReceipt {
    use std::str::FromStr;
    TransactionReceipt {
        block_hash: Hash::from_str(
            "0xce67a85c9fb8bc50213815c32814c159fd75160acf7cb8631e8e7b7cf7f1d472",
        )
        .unwrap(),
        block_number: BlockNumber::new(4190269),
        effective_gas_price: signed_tx.transaction().max_fee_per_gas,
        gas_used: signed_tx.transaction().gas_limit,
        status,
        transaction_hash: signed_tx.hash(),
    }
```
