### Title
CkERC20 Withdrawal with Insufficient `max_transaction_fee` Permanently DoS All Subsequent Withdrawals - (File: rs/ethereum/cketh/minter/src/state/transactions/mod.rs)

---

### Summary

The ckETH/ckERC20 minter assigns sequential Ethereum nonces to withdrawal transactions. When a CkERC20 withdrawal transaction at nonce N cannot be resubmitted because its user-supplied `max_transaction_fee` is too low to cover rising gas prices, `create_resubmit_transactions` halts early and all transactions with nonces > N are never resubmitted. Because Ethereum enforces sequential nonce ordering, all subsequent withdrawals are permanently stuck with no on-chain recovery path and no privileged discard mechanism.

---

### Finding Description

The `EthTransactions` state machine in `rs/ethereum/cketh/minter/src/state/transactions/mod.rs` processes withdrawal requests in strict FIFO order, assigning monotonically increasing Ethereum nonces. [1](#0-0) 

For CkERC20 withdrawals, the user supplies a `max_transaction_fee` field that caps how much ETH the minter may spend on gas for that transaction. The resubmission strategy is `GuaranteeEthAmount`, meaning the minter cannot increase the fee beyond the user-specified cap. [2](#0-1) 

When gas prices rise, `create_resubmit_transactions` iterates over all pending sent transactions in nonce order. On the first CkERC20 transaction whose `max_transaction_fee` is insufficient, it pushes an `Err` and **immediately returns**, explicitly stopping resubmission of all higher-nonce transactions:

```
// We stop on the first error since if a transaction with nonce n could not be resubmitted
// (e.g., the transaction amount does not cover the new fees),
// then the next transactions with nonces n+1, n+2, ... are blocked anyway
``` [3](#0-2) 

The caller `resubmit_transactions_batch` only logs the error and takes no corrective action: [4](#0-3) 

`sent_transactions_to_finalize` only returns transactions whose nonce is strictly less than the Ethereum-reported finalized transaction count. Since nonce N is never mined, the finalized count never advances past N, and all transactions with nonces > N are never finalized: [5](#0-4) 

There is no privileged endpoint to discard or skip a stuck transaction, and no automatic reimbursement path for a transaction that is stuck in `sent_tx` indefinitely.

The same nonce-gap hazard is also acknowledged in `sign_transactions_batch`: [6](#0-5) 

---

### Impact Explanation

Any unprivileged user who submits a CkERC20 withdrawal with a `max_transaction_fee` that is sufficient at submission time but becomes insufficient after a gas price spike will cause:

1. Their transaction at nonce N to be permanently stuck in `sent_tx` (never resubmitted, never mined, never finalized).
2. All subsequent ckETH and ckERC20 withdrawals (nonces N+1, N+2, …) to be permanently stuck on Ethereum, because Ethereum will not mine any transaction with nonce > N until nonce N is resolved.
3. The `MAX_NUM_PENDING_TRANSACTION_NONCES` cap of 1000 to eventually be reached, after which no new withdrawal requests are even batched for processing. [7](#0-6) 

The result is a complete, permanent DoS of the ckETH/ckERC20 withdrawal system with no on-chain recovery path short of a canister upgrade.

---

### Likelihood Explanation

Ethereum gas prices are volatile and can spike 10–100× within minutes during network congestion. A user who submitted a CkERC20 withdrawal with a `max_transaction_fee` calibrated to normal gas prices will naturally trigger this condition during any significant gas spike. This requires no special privileges — any user who has ever submitted a CkERC20 withdrawal is a potential trigger. The condition is also reachable intentionally: an attacker submits a CkERC20 withdrawal with a deliberately minimal `max_transaction_fee` (just enough to pass the initial `InsufficientTransactionFee` check at creation time), then waits for gas prices to rise naturally or during a period of network congestion.

---

### Recommendation

1. **Add a privileged discard endpoint**: Allow a governance-controlled or operator-controlled call to remove a specific stuck transaction from `sent_tx`, reimburse the user for the burned ckERC20 tokens, and allow the nonce sequence to be reset or skipped.
2. **Automatic reimbursement on permanent fee exhaustion**: When `create_resubmit_transactions` returns `ResubmitTransactionError::InsufficientTransactionFee` for a CkERC20 transaction across N consecutive retry cycles, automatically move it to a reimbursement queue rather than leaving it stuck indefinitely.
3. **Decouple nonce assignment from queue position**: Consider assigning nonces only at send time rather than at transaction creation time, so that a stuck request can be skipped without leaving a nonce gap.

---

### Proof of Concept

1. User A submits a CkERC20 withdrawal with `max_transaction_fee = X` (sufficient at current gas prices). The minter creates a transaction at nonce N, signs it, and sends it to Ethereum.
2. Gas prices spike to `> X / gas_limit`.
3. On the next `process_retrieve_eth_requests` timer tick, `resubmit_transactions_batch` calls `create_resubmit_transactions`. At nonce N, `last_signed_tx.resubmit(current_gas_fee)` returns `Err(InsufficientTransactionFee)`. The function returns early. [8](#0-7) 
4. User B, C, D (nonces N+1, N+2, N+3) have their transactions in `sent_tx` but are never resubmitted with competitive fees. Ethereum will not mine them until nonce N is resolved.
5. `finalized_transaction_count` from Ethereum never advances past N. `sent_transactions_to_finalize` returns an empty map for all nonces ≥ N. Users B, C, D are permanently stuck. [9](#0-8) 
6. No canister endpoint exists to discard nonce N or reimburse User A without a full canister upgrade, mirroring the exact DoS pattern from the reported finding.

### Citations

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L346-362)
```rust
/// State machine holding Ethereum transactions issued by the minter.
/// Overall the transaction lifecycle is as follows:
/// 1. The user's withdrawal request is enqueued and processed in a FIFO order.
/// 2. A transaction is created by either consuming a withdrawal request
///    (the first time a transaction is created for that nonce and burn index)
///    or re-submitting an already sent transaction for that nonce and burn index.
/// 3. The transaction is signed via threshold ECDSA and recorded by either consuming the
///    previously created transaction or re-submitting an already sent transaction as is.
/// 4. The transaction is sent to Ethereum. There may have been multiple
///    sent transactions for that nonce and burn index in case of resubmissions.
/// 5. For a given nonce (and burn index), at most one sent transaction is finalized.
///    The others sent transactions for that nonce were never mined and can be discarded.
/// 6. If a given transaction fails the minter will reimburse the user who requested the
///    withdrawal with the corresponding amount minus fees.
#[derive(Clone, Eq, PartialEq, Debug)]
pub struct EthTransactions {
    pub(in crate::state) pending_withdrawal_requests: VecDeque<WithdrawalRequest>,
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L530-537)
```rust
            resubmission: match &withdrawal_request {
                WithdrawalRequest::CkEth(cketh) => ResubmissionStrategy::ReduceEthAmount {
                    withdrawal_amount: cketh.withdrawal_amount,
                },
                WithdrawalRequest::CkErc20(ckerc20) => ResubmissionStrategy::GuaranteeEthAmount {
                    allowed_max_transaction_fee: ckerc20.max_transaction_fee,
                },
            },
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L585-634)
```rust
    /// with nonces greater than the latest mined transaction nonce:
    /// * the resubmitted transaction will need to be re-signed if its transaction fee was increased
    /// * the resubmitted transaction can be resent as is if its transaction fee was not increased
    ///
    /// We stop on the first error since if a transaction with nonce n could not be resubmitted
    /// (e.g., the transaction amount does not cover the new fees),
    /// then the next transactions with nonces n+1, n+2, ... are blocked anyway
    /// and trying to resubmit them would only artificially increase their transaction fees.
    pub fn create_resubmit_transactions(
        &self,
        latest_transaction_count: TransactionCount,
        current_gas_fee: GasFeeEstimate,
    ) -> Vec<Result<(LedgerBurnIndex, Eip1559TransactionRequest), ResubmitTransactionError>> {
        // If transaction count at block height H is c > 0, then transactions with nonces
        // 0, 1, ..., c - 1 were mined. If transaction count is 0, then no transactions were mined.
        // The nonce of the first pending transaction is then exactly c.
        let first_pending_tx_nonce: TransactionNonce = latest_transaction_count.change_units();
        let mut transactions_to_resubmit = Vec::new();
        for (nonce, burn_index, signed_tx) in self
            .sent_tx
            .iter()
            .filter(|(nonce, _burn_index, _signed_tx)| *nonce >= &first_pending_tx_nonce)
        {
            let last_signed_tx = signed_tx.last().expect("BUG: empty sent transactions list");
            match last_signed_tx.resubmit(current_gas_fee.clone()) {
                Ok(Some(new_tx)) => {
                    transactions_to_resubmit.push(Ok((*burn_index, new_tx)));
                }
                Ok(None) => {
                    // the transaction fee is still up-to-date but because the transaction did not get included,
                    // we re-send it as is to be sure that it remains known to the mempool and hopefully be included at some point.
                    // Since we always re-send the last non-included transactions in sent_tx, there is nothing to do.
                }
                Err(crate::tx::ResubmitTransactionError::InsufficientTransactionFee {
                    allowed_max_transaction_fee,
                    actual_max_transaction_fee,
                }) => {
                    transactions_to_resubmit.push(Err(
                        ResubmitTransactionError::InsufficientTransactionFee {
                            ledger_burn_index: *burn_index,
                            transaction_nonce: *nonce,
                            allowed_max_transaction_fee,
                            max_transaction_fee: actual_max_transaction_fee,
                        },
                    ));
                    return transactions_to_resubmit;
                }
            }
        }
        transactions_to_resubmit
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L654-678)
```rust
    pub fn sent_transactions_to_finalize(
        &self,
        finalized_transaction_count: &TransactionCount,
    ) -> BTreeMap<Hash, LedgerBurnIndex> {
        let first_non_finalized_tx_nonce: TransactionNonce =
            finalized_transaction_count.change_units();
        let mut transactions = BTreeMap::new();
        for (_nonce, index, sent_txs) in self
            .sent_tx
            .iter()
            .filter(|(nonce, _burn_index, _signed_txs)| *nonce < &first_non_finalized_tx_nonce)
        {
            for sent_tx in sent_txs {
                if let Some(prev_index) = transactions.insert(sent_tx.as_ref().hash(), *index) {
                    assert_eq!(
                        prev_index,
                        *index,
                        "BUG: duplicate transaction hash {} for burn indices {prev_index} and {index}",
                        sent_tx.as_ref().hash()
                    );
                }
            }
        }
        transactions
    }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L911-922)
```rust
        const MAX_NUM_PENDING_TRANSACTION_NONCES: usize = 1000;
        let unique_pending_transaction_nonces: BTreeSet<_> =
            self.created_tx.keys().chain(self.sent_tx.keys()).collect();
        let actual_batch_size = min(
            MAX_NUM_PENDING_TRANSACTION_NONCES
                .saturating_sub(unique_pending_transaction_nonces.len()),
            requested_batch_size,
        );
        self.withdrawal_requests_iter()
            .take(actual_batch_size)
            .cloned()
            .collect()
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L225-246)
```rust
    for result in transactions_to_resubmit {
        match result {
            Ok((withdrawal_id, transaction)) => {
                log!(
                    INFO,
                    "[resubmit_transactions_batch]: transactions to resubmit {transaction:?}"
                );
                mutate_state(|s| {
                    process_event(
                        s,
                        EventType::ReplacedTransaction {
                            withdrawal_id,
                            transaction,
                        },
                    )
                });
            }
            Err(e) => {
                log!(INFO, "Failed to resubmit transaction: {e:?}");
            }
        }
    }
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L330-338)
```rust
    if !errors.is_empty() {
        // At this point there might be a gap in transaction nonces between signed transactions, e.g.,
        // transactions 1,2,4,5 were signed, but 3 was not due to some unexpected error.
        // This means that transactions 4 and 5 are currently stuck until transaction 3 is signed.
        // However, we still proceed with transactions 4 and 5 since that way they might be mined faster
        // once transaction 3 is sent on the next iteration. Otherwise, we would need to re-sign transactions 4 and 5
        // and send them (together with transaction 3) on the next iteration.
        log!(INFO, "Errors encountered during signing: {errors:?}");
    }
```
