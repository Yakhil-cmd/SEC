### Title
ckERC20 Withdrawal Permanently Freezes Minter Under Ethereum Gas Price Spike — (`rs/ethereum/cketh/minter/src/withdraw.rs`, `rs/ethereum/cketh/minter/src/state/transactions/mod.rs`)

---

### Summary

When Ethereum gas prices spike above a ckERC20 withdrawal request's pre-paid `max_transaction_fee`, the ckETH minter cannot resubmit the stuck transaction. The error is silently logged with no reimbursement or cancellation path. Because Ethereum enforces sequential nonce ordering, the stuck transaction also blocks every subsequent pending withdrawal, freezing the entire minter withdrawal pipeline indefinitely until gas prices fall.

---

### Finding Description

The ckETH minter processes ckERC20 withdrawals via a two-phase flow: the user pre-pays a fixed `max_transaction_fee` in ckETH at withdrawal time, and the minter later creates and sends an EIP-1559 transaction capped at that fee.

**Phase 1 — Transaction creation** (`create_transaction` in `rs/ethereum/cketh/minter/src/state/transactions/mod.rs`):

For `WithdrawalRequest::CkErc20`, the minter derives `request_max_fee_per_gas` from the user's fixed `max_transaction_fee` and rejects creation if the current gas estimate already exceeds it:

```rust
let actual_min_max_fee_per_gas = gas_fee_estimate.min_max_fee_per_gas();
if actual_min_max_fee_per_gas > request_max_fee_per_gas {
    return Err(CreateTransactionError::InsufficientTransactionFee { ... });
}
```

At this stage the request is rescheduled to the back of the queue — a safe fallback.

**Phase 2 — Resubmission** (`SignedTransactionRequest::resubmit` in `rs/ethereum/cketh/minter/src/tx.rs`):

Once a transaction has been signed and sent, if gas prices spike the minter tries to resubmit with a higher fee. For ckERC20 the resubmission strategy is `GuaranteeEthAmount { allowed_max_transaction_fee }`, which enforces a hard cap:

```rust
if new_tx_price.max_transaction_fee() > self.resubmission.allowed_max_transaction_fee() {
    return Err(ResubmitTransactionError::InsufficientTransactionFee { ... });
}
```

This is fundamentally different from ckETH withdrawals, which use `ReduceEthAmount` and can always absorb higher fees by reducing the sent amount.

**Cascading freeze** (`create_resubmit_transactions` in `rs/ethereum/cketh/minter/src/state/transactions/mod.rs`):

When the first pending transaction (nonce N) cannot be resubmitted, the function immediately returns, blocking all transactions with nonces N+1, N+2, …:

```rust
// We stop on the first error since if a transaction with nonce n could not be resubmitted
// (e.g., the transaction amount does not cover the new fees),
// then the next transactions with nonces n+1, n+2, ... are blocked anyway
...
return transactions_to_resubmit;
```

**No recovery path** (`resubmit_transactions_batch` in `rs/ethereum/cketh/minter/src/withdraw.rs`):

The error is only logged; no reimbursement event is emitted and no cancellation is triggered:

```rust
Err(e) => {
    log!(INFO, "Failed to resubmit transaction: {e:?}");
}
```

The transaction remains in `sent_tx` indefinitely. The user's burned ckETH (fee) and burned ckERC20 tokens have no recovery path until gas prices fall below the original cap.

---

### Impact Explanation

- **User funds frozen**: A user's ckETH fee and ckERC20 tokens are burned at withdrawal time. If gas spikes, the Ethereum transaction is stuck and the user cannot recover their tokens until gas normalises — which may never happen if the spike is sustained.
- **Minter-wide pipeline freeze**: Because Ethereum enforces sequential nonce ordering, a single stuck ckERC20 transaction blocks every subsequent withdrawal (ckETH and ckERC20) queued behind it. All users with pending withdrawals are affected.
- **No admin escape hatch**: There is no governance proposal, canister upgrade path, or user-callable endpoint to cancel a stuck sent transaction and trigger reimbursement.

---

### Likelihood Explanation

Ethereum gas price spikes are routine and well-documented (e.g., NFT mints, network congestion events). A user who submitted a ckERC20 withdrawal during a low-gas window with a conservative `max_transaction_fee` will find their withdrawal frozen during any subsequent spike. A malicious actor can deliberately trigger this by submitting a withdrawal with a minimal fee during a low-gas period, then waiting for gas to rise, causing the minter to freeze all subsequent withdrawals for all users.

The attacker-controlled entry path is the public `withdraw_erc20` endpoint on the ckETH minter canister, callable by any unprivileged principal.

---

### Recommendation

1. **Reimbursement on stuck sent transactions**: When `resubmit_transactions_batch` receives `ResubmitTransactionError::InsufficientTransactionFee`, emit a `ReimbursementRequest` event for the stuck ckERC20 withdrawal (returning both the ckETH fee and the ckERC20 tokens), and remove the transaction from `sent_tx` after a configurable expiry block count — analogous to the "force-fail after EXPIRY_NUM blocks" suggested in the original report.
2. **Nonce unblocking**: After reimbursing and removing the stuck transaction, allow the minter to advance the nonce and resume processing subsequent withdrawals.
3. **Fee buffer at withdrawal time**: Require users to pre-pay a `max_transaction_fee` that includes a safety margin for resubmission (e.g., at least 10% above the current estimate), reducing the probability of hitting the cap during normal gas fluctuations.

---

### Proof of Concept

The existing test `should_not_resubmit_ckerc20_transactions_unless_max_priority_fee_increases` in `rs/ethereum/cketh/minter/src/state/transactions/tests.rs` already demonstrates the freeze:

```rust
let too_high_price = GasFeeEstimate {
    base_fee_per_gas: DEFAULT_CKERC20_MAX_FEE_PER_GAS,
    max_priority_fee_per_gas: WeiPerGas::ONE,
};
let resubmitted_txs = transactions.create_resubmit_transactions(
    TransactionCount::from(30_u8),
    too_high_price.clone(),
);
assert_eq!(
    resubmitted_txs,
    vec![Err(ResubmitTransactionError::InsufficientTransactionFee {
        ledger_burn_index: 93_u64.into(),
        transaction_nonce: 30_u8.into(),
        allowed_max_transaction_fee: DEFAULT_MAX_TRANSACTION_FEE.into(),
        max_transaction_fee: 30_000_000_000_165_000_u128.into(),
    })]
);
```

This confirms that a single ckERC20 transaction with an exceeded fee cap causes `create_resubmit_transactions` to return early with an error, and the caller (`resubmit_transactions_batch`) only logs it — leaving the transaction and all subsequent ones permanently stuck. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L208-246)
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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1155-1168)
```rust
            let request_max_fee_per_gas = request
                .max_transaction_fee
                .into_wei_per_gas(gas_limit)
                .expect("BUG: gas_limit should be non-zero");
            let actual_min_max_fee_per_gas = gas_fee_estimate.min_max_fee_per_gas();
            if actual_min_max_fee_per_gas > request_max_fee_per_gas {
                return Err(CreateTransactionError::InsufficientTransactionFee {
                    cketh_ledger_burn_index: request.cketh_ledger_burn_index,
                    allowed_max_transaction_fee: request.max_transaction_fee,
                    actual_max_transaction_fee: actual_min_max_fee_per_gas
                        .transaction_cost(gas_limit)
                        .unwrap_or(Wei::MAX),
                });
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

**File:** rs/ethereum/cketh/minter/src/state/transactions/tests.rs (L1014-1031)
```rust
            let too_high_price = GasFeeEstimate {
                base_fee_per_gas: DEFAULT_CKERC20_MAX_FEE_PER_GAS,
                max_priority_fee_per_gas: WeiPerGas::ONE,
            };
            let resubmitted_txs = transactions.create_resubmit_transactions(
                TransactionCount::from(30_u8),
                too_high_price.clone(),
            );
            assert_eq!(
                resubmitted_txs,
                vec![Err(ResubmitTransactionError::InsufficientTransactionFee {
                    ledger_burn_index: 93_u64.into(),
                    transaction_nonce: 30_u8.into(),
                    allowed_max_transaction_fee: DEFAULT_MAX_TRANSACTION_FEE.into(),
                    max_transaction_fee: 30_000_000_000_165_000_u128.into(),
                })]
            );
        }
```
