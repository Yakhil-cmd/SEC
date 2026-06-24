### Title
ckETH Minter Sequential Nonce Assignment with No Cancel/Skip Mechanism Causes Permanent Head-of-Line Blocking of All Subsequent Withdrawals - (`rs/ethereum/cketh/minter/src/state/transactions/mod.rs`)

---

### Summary

The ckETH minter assigns strictly sequential Ethereum nonces to withdrawal requests processed from a FIFO `VecDeque`. When a sent transaction's `max_transaction_fee` cap is too low to cover a bumped gas price during resubmission, `create_resubmit_transactions` returns early with `ResubmitTransactionError::InsufficientTransactionFee` and the minter has no mechanism to cancel, skip, or quarantine the stuck nonce. Because Ethereum enforces sequential nonce ordering, the stuck nonce permanently blocks finalization of every subsequent withdrawal (nonces N+1, N+2, …) until gas prices fall below the stuck transaction's fee cap or a canister upgrade is performed. The code itself acknowledges this in a comment: *"if a transaction with nonce n could not be resubmitted … then the next transactions with nonces n+1, n+2, … are blocked anyway."*

---

### Finding Description

**Root cause — `create_resubmit_transactions` early-returns on the first fee-cap breach:** [1](#0-0) 

The function iterates over all pending (unmined) sent transactions in nonce order. When the current gas fee estimate exceeds the user-supplied `max_transaction_fee` for nonce N, it pushes an `Err(ResubmitTransactionError::InsufficientTransactionFee)` and immediately `return`s, leaving nonces N+1, N+2, … unprocessed.

**The caller silently logs and discards the error:** [2](#0-1) 

`resubmit_transactions_batch` iterates the returned `Vec`, logs the error for the stuck entry, and does nothing else. The stuck transaction remains in `sent_tx` with its original (too-low) fee cap indefinitely.

**Finalization is gated on the Ethereum finalized transaction count:** [3](#0-2) 

`sent_transactions_to_finalize` only considers nonces strictly below `finalized_transaction_count`. Because Ethereum will not mine nonce N+1 before nonce N is mined, the finalized count never advances past N, so no subsequent withdrawal can ever be finalized.

**The FIFO queue assigns nonces sequentially:** [4](#0-3) 

`pending_withdrawal_requests: VecDeque<WithdrawalRequest>` is consumed front-to-back; each request receives `next_nonce` which is then incremented. [5](#0-4) 

**The `InsufficientTransactionFee` path in `SignedTransactionRequest::resubmit`:** [6](#0-5) 

For ckERC20 withdrawals the `allowed_max_transaction_fee` is the user-supplied `max_transaction_fee` field. If the new gas estimate requires a higher fee, resubmission is rejected and the transaction stays in `sent_tx` with its original low fee cap.

**The sign-batch step also acknowledges the downstream blocking:** [7](#0-6) 

The comment explicitly states that a signing failure for nonce N leaves nonces N+1, N+2, … stuck.

---

### Impact Explanation

A single ckERC20 withdrawal request whose `max_transaction_fee` is insufficient to cover a gas-price spike permanently blocks every subsequent ckETH and ckERC20 withdrawal from being finalized on Ethereum. Users behind the stuck request cannot receive their funds. The minter has no admin function, no skip/quarantine path, and no cancel-and-reimburse path for the stuck nonce; resolution requires a canister upgrade. This matches the severity of the BasisTradeVault analog: indefinite withdrawal queue halt for all users behind the stuck entry.

---

### Likelihood Explanation

Any unprivileged user who calls `withdraw_erc20` with a `max_transaction_fee` that is valid at submission time but insufficient during a subsequent gas-price spike can trigger this condition. Ethereum gas prices are volatile; spikes of 5–10× within hours are historically common. An adversary can deliberately set a minimal `max_transaction_fee` (just enough to pass the initial `create_transaction` check) and wait for natural congestion, or time the submission to coincide with a known high-activity event. No privileged access, no threshold corruption, and no external oracle manipulation is required beyond normal Ethereum gas-price volatility.

---

### Recommendation

1. **Add a cancel-and-reimburse path for stuck nonces.** When `create_resubmit_transactions` returns `InsufficientTransactionFee` for nonce N, the minter should be able to cancel that withdrawal (burn the nonce by sending a zero-value self-transfer at the current gas price, funded from the minter's ETH reserve), remove the entry from `sent_tx`, and reimburse the user's ckERC20 tokens. This unblocks nonces N+1, N+2, ….

2. **Alternatively, expose an operator-callable `cancel_withdrawal` endpoint** that performs the same nonce-burn and reimbursement under governance control, analogous to the ckBTC minter's upgrade-based unstuck mechanism.

3. **Enforce a minimum `max_transaction_fee`** at withdrawal submission time (e.g., a multiple of the current gas estimate) to reduce the probability of a fee cap being breached during normal volatility.

---

### Proof of Concept

1. Ethereum mainnet gas price is 10 gwei. User calls `withdraw_erc20` with `max_transaction_fee` = 650 000 gwei (barely covers 10 gwei × 65 000 gas limit). The minter creates a transaction with nonce N and sends it.

2. Gas price spikes to 15 gwei. The minter calls `create_resubmit_transactions(latest_tx_count, new_gas_fee)`. For nonce N, `new_tx_price.max_transaction_fee()` = 975 000 gwei > 650 000 gwei = `allowed_max_transaction_fee`. The function pushes `Err(InsufficientTransactionFee)` and returns immediately. [8](#0-7) 

3. `resubmit_transactions_batch` logs the error and does nothing. Nonce N remains in `sent_tx` with the original 10-gwei fee cap. Ethereum will not mine it at 15 gwei.

4. All subsequent withdrawal requests (nonces N+1, N+2, …) are in `sent_tx` but `sent_transactions_to_finalize` filters them out because `finalized_transaction_count` is still N (Ethereum has not mined nonce N). [9](#0-8) 

5. The queue is permanently halted. Every timer tick re-runs `resubmit_transactions_batch`, hits the same error for nonce N, and exits. No subsequent withdrawal is ever finalized until gas prices drop below 10 gwei or a canister upgrade intervenes.

### Citations

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L361-377)
```rust
pub struct EthTransactions {
    pub(in crate::state) pending_withdrawal_requests: VecDeque<WithdrawalRequest>,
    // Processed withdrawal requests (transaction created, sent, or finalized).
    pub(in crate::state) processed_withdrawal_requests:
        BTreeMap<LedgerBurnIndex, WithdrawalRequest>,
    pub(in crate::state) created_tx:
        MultiKeyMap<TransactionNonce, LedgerBurnIndex, TransactionRequest>,
    pub(in crate::state) sent_tx:
        MultiKeyMap<TransactionNonce, LedgerBurnIndex, Vec<SignedTransactionRequest>>,
    pub(in crate::state) finalized_tx:
        MultiKeyMap<TransactionNonce, LedgerBurnIndex, FinalizedEip1559Transaction>,
    pub(in crate::state) next_nonce: TransactionNonce,

    pub(in crate::state) maybe_reimburse: BTreeSet<LedgerBurnIndex>,
    pub(in crate::state) reimbursement_requests: BTreeMap<ReimbursementIndex, ReimbursementRequest>,
    pub(in crate::state) reimbursed: BTreeMap<ReimbursementIndex, ReimbursedResult>,
}
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L521-527)
```rust
        let nonce = self.next_nonce;
        assert_eq!(transaction.nonce, nonce, "BUG: transaction nonce mismatch");
        self.next_nonce = self
            .next_nonce
            .checked_increment()
            .expect("Transaction nonce overflow");
        self.remove_withdrawal_request(&withdrawal_request);
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L584-635)
```rust
    /// Create transactions to resubmit corresponding to already sent transactions
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
    }
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

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L208-247)
```rust
async fn resubmit_transactions_batch(
    latest_transaction_count: Option<TransactionCount>,
    gas_fee_estimate: &GasFeeEstimate,
) {
    if read_state(|s| s.eth_transactions.is_sent_tx_empty()) {
        return;
    }
    let latest_transaction_count = match latest_transaction_count {
        Some(latest_transaction_count) => latest_transaction_count,
        None => {
            return;
        }
    };
    let transactions_to_resubmit = read_state(|s| {
        s.eth_transactions
            .create_resubmit_transactions(latest_transaction_count, gas_fee_estimate.clone())
    });
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

**File:** rs/ethereum/cketh/minter/src/tx.rs (L155-189)
```rust
impl SignedTransactionRequest {
    pub fn resubmit(
        &self,
        new_gas_fee: GasFeeEstimate,
    ) -> Result<Option<Eip1559TransactionRequest>, ResubmitTransactionError> {
        let transaction_request = self.transaction.transaction();
        let last_tx_price = transaction_request.transaction_price();
        let new_tx_price = last_tx_price
            .clone()
            .resubmit_transaction_price(new_gas_fee);
        if new_tx_price == last_tx_price {
            return Ok(None);
        }

        if new_tx_price.max_transaction_fee() > self.resubmission.allowed_max_transaction_fee() {
            return Err(ResubmitTransactionError::InsufficientTransactionFee {
                allowed_max_transaction_fee: self.resubmission.allowed_max_transaction_fee(),
                actual_max_transaction_fee: new_tx_price.max_transaction_fee(),
            });
        }
        let new_amount = match self.resubmission {
            ResubmissionStrategy::ReduceEthAmount { withdrawal_amount } => {
                withdrawal_amount.checked_sub(new_tx_price.max_transaction_fee())
                    .expect("BUG: withdrawal_amount covers new transaction fee because it was checked before")
            }
            ResubmissionStrategy::GuaranteeEthAmount { .. } => transaction_request.amount,
        };
        Ok(Some(Eip1559TransactionRequest {
            max_priority_fee_per_gas: new_tx_price.max_priority_fee_per_gas,
            max_fee_per_gas: new_tx_price.max_fee_per_gas,
            gas_limit: new_tx_price.gas_limit,
            amount: new_amount,
            ..transaction_request.clone()
        }))
    }
```
