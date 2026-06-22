### Title
Inadequate Tracking of Permanently Stuck ckETH Withdrawal Transactions When `ResubmitTransactionError::InsufficientTransactionFee` Occurs - (File: rs/ethereum/cketh/minter/src/withdraw.rs)

---

### Summary

In the ckETH minter, when `resubmit_transactions_batch` encounters a `ResubmitTransactionError::InsufficientTransactionFee` error (gas fees have risen above the user's allowed maximum), the error is only logged. No reimbursement is scheduled, no "stuck" state is recorded, and the transaction remains in `sent_tx` indefinitely. Because `create_resubmit_transactions` explicitly stops at the first such error (all higher-nonce transactions are blocked by it), a single permanently-stuck transaction halts all subsequent ckETH/ckERC20 withdrawal processing with no recovery path for affected users.

---

### Finding Description

**Root cause — `resubmit_transactions_batch`** [1](#0-0) 

When `create_resubmit_transactions` returns `Err(ResubmitTransactionError::InsufficientTransactionFee { … })`, the caller does nothing except emit a log line:

```rust
Err(e) => {
    log!(INFO, "Failed to resubmit transaction: {e:?}");
}
```

No state mutation occurs. The transaction stays in `sent_tx`. No reimbursement is enqueued.

**Why the error is terminal for that nonce and all higher nonces**

`create_resubmit_transactions` documents and implements an early-return on the first error: [2](#0-1) 

> "We stop on the first error since if a transaction with nonce n could not be resubmitted … then the next transactions with nonces n+1, n+2, … are blocked anyway."

So a single stuck transaction freezes the entire outbound nonce sequence.

**How the fee cap is enforced**

For ckETH withdrawals the resubmission cap is the original `withdrawal_amount`; for ckERC20 it is `max_transaction_fee`. Once the current gas fee estimate exceeds that cap, `SignedTransactionRequest::resubmit` returns `InsufficientTransactionFee`: [3](#0-2) 

**Contrast with the handled case in `create_transactions_batch`**

When a *pending* (not yet sent) withdrawal request cannot be turned into a transaction due to insufficient fee, it is correctly rescheduled: [4](#0-3) 

The analogous recovery action is entirely absent for already-sent transactions.

**No finalization path for permanently stuck transactions**

`finalize_transactions_batch` only processes transactions whose nonces are below the on-chain finalized count. A transaction that is never mined is never finalized, so there is no automatic cleanup.

---

### Impact Explanation

1. **User funds locked** — the ckETH/ckERC20 that was burned on the ledger is not delivered to Ethereum and no reimbursement is ever scheduled. The `pending_withdrawal_reimbursements` map is never populated for the affected burn index.
2. **Withdrawal queue frozen** — because `create_resubmit_transactions` stops at the first error, all subsequent withdrawal requests (higher nonces) are also blocked. The minter's entire outbound pipeline stalls.
3. **No operator escape hatch** — there is no admin endpoint to force-reimburse or remove a stuck `sent_tx` entry without a canister upgrade.

---

### Likelihood Explanation

Ethereum gas prices are volatile. A user who submits a ckETH withdrawal with a small amount (e.g., just above the minimum) during a low-fee period will have their transaction stuck if fees spike significantly before the transaction is mined. This is a realistic, non-adversarial scenario. A malicious actor could also deliberately submit a minimal-amount withdrawal and then wait for (or contribute to) a gas spike to trigger the freeze. The entry path requires only an unprivileged `withdraw_eth` call followed by an Ethereum gas price increase — both are externally reachable conditions.

---

### Recommendation

When `ResubmitTransactionError::InsufficientTransactionFee` is returned in `resubmit_transactions_batch`, the minter should:

1. Schedule a reimbursement for the affected burn index via `schedule_withdrawal_reimbursement` (mirroring the existing `pending_withdrawal_reimbursements` flow used for failed transactions).
2. Remove the stuck entry from `sent_tx` so that subsequent nonces are unblocked.
3. Emit an audit event so the event-log replay can reconstruct the correct state.

The existing `quarantine_withdrawal_reimbursement` / `reimburse_withdrawal_completed` machinery in `rs/ethereum/cketh/minter/src/state/transactions/mod.rs` already provides the scaffolding needed. [5](#0-4) 

---

### Proof of Concept

1. User calls `withdraw_eth` with `withdrawal_amount` = X Wei (just above the minimum), burning X ckETH. The request enters `pending_withdrawal_requests`.
2. `create_transactions_batch` creates an EIP-1559 transaction with nonce N and records it in `sent_tx`. The `ResubmissionStrategy::ReduceEthAmount { withdrawal_amount: X }` cap is set.
3. Ethereum base fee spikes so that `new_tx_price.max_transaction_fee() > X`.
4. On the next timer tick, `resubmit_transactions_batch` calls `create_resubmit_transactions`. For nonce N, `SignedTransactionRequest::resubmit` returns `Err(InsufficientTransactionFee)`. The function returns immediately, leaving nonces N, N+1, N+2, … all unprocessable.
5. `resubmit_transactions_batch` logs the error and returns. No entry is added to `pending_withdrawal_reimbursements`.
6. `send_transactions_batch` continues to broadcast the original (low-fee) transaction, but it is never mined because the fee is below the current base fee.
7. All subsequent ckETH/ckERC20 withdrawals submitted after nonce N are permanently blocked. The user's X Wei equivalent is locked with no reimbursement path. [6](#0-5) [7](#0-6)

### Citations

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

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L281-291)
```rust
            Err(CreateTransactionError::InsufficientTransactionFee {
                cketh_ledger_burn_index: ledger_burn_index,
                allowed_max_transaction_fee: withdrawal_amount,
                actual_max_transaction_fee: max_transaction_fee,
            }) => {
                log!(
                    INFO,
                    "[create_transactions_batch]: Withdrawal request with burn index {ledger_burn_index} has insufficient amount {withdrawal_amount:?} to cover transaction fees: {max_transaction_fee:?}. Request moved back to end of queue."
                );
                mutate_state(|s| s.eth_transactions.reschedule_withdrawal_request(request));
            }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L346-377)
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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L589-635)
```rust
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

**File:** rs/ethereum/cketh/minter/src/tx.rs (L155-174)
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
```
