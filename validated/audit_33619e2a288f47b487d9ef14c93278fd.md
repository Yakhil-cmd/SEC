### Title
No Recovery Mechanism for Stuck ckERC20 Withdrawals When Gas Fees Exceed the Pre-Approved Fee Cap - (File: `rs/ethereum/cketh/minter/src/withdraw.rs`)

---

### Summary

The ckETH minter has no user-accessible or automated recovery function for ckERC20 withdrawal transactions that become permanently stuck when Ethereum gas fees spike above the `max_transaction_fee` cap that was estimated at withdrawal time. This is the direct IC analog of the missing `resendSetWeightMessage` function described in the external report: a multi-step cross-chain flow can freeze with no on-chain escape hatch, locking both burned ckETH (gas) and burned ckERC20 tokens indefinitely.

---

### Finding Description

The ckERC20 withdrawal flow in the ckETH minter is:

1. User calls `withdraw_erc20`; the minter calls `estimate_erc20_transaction_fee()` and uses the result as the immutable `max_transaction_fee` cap for the lifetime of the withdrawal.
2. The minter burns ckETH (for gas) and ckERC20 (for the withdrawal amount).
3. An EIP-1559 transaction is created, threshold-ECDSA signed, and placed in `sent_tx`.
4. On every timer tick, `process_retrieve_eth_requests` calls `resubmit_transactions_batch`, which calls `create_resubmit_transactions`.

Inside `create_resubmit_transactions`:

```rust
// rs/ethereum/cketh/minter/src/state/transactions/mod.rs:593-634
pub fn create_resubmit_transactions(
    &self,
    latest_transaction_count: TransactionCount,
    current_gas_fee: GasFeeEstimate,
) -> Vec<Result<(LedgerBurnIndex, Eip1559TransactionRequest), ResubmitTransactionError>> {
    ...
    for (nonce, burn_index, signed_tx) in self.sent_tx.iter()
        .filter(|(nonce, ..)| *nonce >= &first_pending_tx_nonce)
    {
        match last_signed_tx.resubmit(current_gas_fee.clone()) {
            ...
            Err(crate::tx::ResubmitTransactionError::InsufficientTransactionFee { .. }) => {
                transactions_to_resubmit.push(Err(ResubmitTransactionError::InsufficientTransactionFee { .. }));
                return transactions_to_resubmit;   // ← hard stop; all higher nonces also blocked
            }
        }
    }
    ...
}
``` [1](#0-0) 

The `resubmit` call in `rs/ethereum/cketh/minter/src/tx.rs` returns `Err(InsufficientTransactionFee)` whenever the new gas price would push the total fee above `allowed_max_transaction_fee` (which equals the `max_transaction_fee` burned at withdrawal time):

```rust
// rs/ethereum/cketh/minter/src/tx.rs:169-173
if new_tx_price.max_transaction_fee() > self.resubmission.allowed_max_transaction_fee() {
    return Err(ResubmitTransactionError::InsufficientTransactionFee { ... });
}
``` [2](#0-1) 

Back in `resubmit_transactions_batch`, the error is only logged — no reimbursement, no cancellation, no escalation:

```rust
// rs/ethereum/cketh/minter/src/withdraw.rs:242-244
Err(e) => {
    log!(INFO, "Failed to resubmit transaction: {e:?}");
}
``` [3](#0-2) 

The code comment explicitly acknowledges the nonce-cascade freeze:

> "We stop on the first error since if a transaction with nonce n could not be resubmitted … then the next transactions with nonces n+1, n+2, … are blocked anyway." [4](#0-3) 

The `send_transactions_batch` function continues to broadcast the original (low-fee) signed transaction, but if the Ethereum base fee never drops back below the original `max_fee_per_gas`, the transaction is never mined. The `finalize_transactions_batch` path only triggers reimbursement on an on-chain `TransactionStatus::Failure` receipt — a transaction that is never mined produces no receipt and no reimbursement. [5](#0-4) 

There is no `cancel_withdrawal`, `force_reimburse`, or equivalent endpoint in the minter's public interface. [6](#0-5) 

---

### Impact Explanation

| Asset | Outcome |
|---|---|
| ckETH burned for gas | Locked until the transaction is mined or gas fees fall below the original estimate |
| ckERC20 burned for withdrawal | Same — no reimbursement path while the transaction is in `sent_tx` |
| All subsequent withdrawals (higher nonces) | Blocked from fee-bump resubmission by the nonce-cascade hard stop |

The ckBTC minter experienced an identical class of real-world stuck-withdrawal incidents (documented in `rs/bitcoin/ckbtc/mainnet/minter_upgrade_2025_06_27.md`) that required emergency governance upgrades to unblock users. [7](#0-6) 

---

### Likelihood Explanation

- Any unprivileged user can call `withdraw_erc20`; the `max_transaction_fee` cap is set by the minter's internal fee estimate, not by the user, so no special knowledge is required.
- Ethereum gas fees are volatile; spikes of 2–10× within minutes are historically common during network congestion.
- The minter's safety margin (≥10% headroom per resubmission) is insufficient against sustained fee spikes.
- The ckBTC minter's real-world stuck-transaction incidents confirm this class of failure is reachable in production.

---

### Recommendation

Add a recovery function analogous to `resendRegisterValidatorMessage` / `resendEndValidatorMessage` in the external report. Concretely:

1. **Automated timeout reimbursement**: If a withdrawal has been in `sent_tx` for longer than a configurable threshold (e.g., 7 days) and cannot be resubmitted due to `InsufficientTransactionFee`, automatically move it to `reimbursement_requests` so the user's ckETH and ckERC20 are returned.
2. **Admin/governance cancel endpoint**: A privileged endpoint (callable by NNS governance) to forcibly cancel a specific stuck withdrawal by `ledger_burn_index` and schedule reimbursement — mirroring the emergency upgrade pattern already used for ckBTC.

---

### Proof of Concept

1. User calls `withdraw_erc20`; minter estimates `max_transaction_fee = F` and burns ckETH + ckERC20.
2. Transaction with nonce N is placed in `sent_tx` with `allowed_max_transaction_fee = F`.
3. Ethereum base fee spikes; new required fee = `1.5 × F > F`.
4. On the next timer tick, `create_resubmit_transactions` hits `InsufficientTransactionFee` for nonce N and returns immediately — nonces N+1, N+2, … are also skipped.
5. `resubmit_transactions_batch` logs the error and exits; no state change occurs.
6. `send_transactions_batch` re-broadcasts the original low-fee transaction, which is rejected by Ethereum nodes as underpriced.
7. The transaction is never mined; `finalize_transactions_batch` never sees a receipt; no reimbursement is triggered.
8. The user's ckETH and ckERC20 remain locked with no callable recovery path. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L584-634)
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

**File:** rs/ethereum/cketh/minter/src/tx.rs (L169-173)
```rust
        if new_tx_price.max_transaction_fee() > self.resubmission.allowed_max_transaction_fee() {
            return Err(ResubmitTransactionError::InsufficientTransactionFee {
                allowed_max_transaction_fee: self.resubmission.allowed_max_transaction_fee(),
                actual_max_transaction_fee: new_tx_price.max_transaction_fee(),
            });
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

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L337-349)
```text
type WithdrawalStatus = variant {
    // Request is pending, i.e. transaction is not yet created.
    Pending;

    // Transaction created byt not yet sent.
    TxCreated;

    // Transaction sent but not yet finalized.
    TxSent : EthTransaction;

    // Transaction already finalized.
    TxFinalized : TxFinalizedStatus;
};
```

**File:** rs/bitcoin/ckbtc/mainnet/minter_upgrade_2025_06_27.md (L19-33)
```markdown
Upgrade the ckBTC minter to try to unblock three transactions ckBTC → BTC (withdrawals) that are currently stuck since
2025.06.21.

After analysis, see this
forum [**post**](https://forum.dfinity.org/t/ckbtc-a-canister-issued-bitcoin-twin-token-on-the-ic-1-1-backed-by-btc/17606/202)
for more details, the problem appears to be due to the following:

1. An extremely low fee per vbyte was chosen by the minter for those transactions, which prevented them from being mined
   in the first place. We currently don’t have a satisfying explanation for how this low median fee was computed and are
   also investigating the bitcoin canister. A stop-gap solution was introduced
   in [#5742](https://github.com/dfinity/ic/pull/5742), to ensure that the fee per vbyte computed by the minter is
   always at least 1.5 sats/vbyte (for Bitcoin Mainnet).
2. There is a deterministic panic occurring in the minter when it tries to resubmit those transactions, which explains
   why those transactions are currently stuck. This should be completely fixed
   by [#5713](https://github.com/dfinity/ic/pull/5713).
```
