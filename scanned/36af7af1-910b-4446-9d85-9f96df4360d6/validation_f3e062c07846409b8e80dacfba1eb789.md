### Title
ckETH Minter Withdrawal Permanently Stuck When Gas Fees Spike Beyond Withdrawal Amount — (File: `rs/ethereum/cketh/minter/src/tx.rs`)

---

### Summary

The ckETH minter's two-phase withdrawal process records `withdrawal_amount` at request time but enforces a strict fee ceiling at resubmission time. If Ethereum gas fees spike between when the transaction was first sent and when resubmission is attempted, the strict check in `SignedTransactionRequest::resubmit()` returns `ResubmitTransactionError::InsufficientTransactionFee`. The caller in `resubmit_transactions_batch` only logs this error — no reimbursement is triggered — leaving the user's ckETH permanently burned with no ETH received and no recovery path short of a governance upgrade.

---

### Finding Description

The ckETH minter implements a multi-phase withdrawal:

**Phase 1 — Request:** The user calls `withdraw_eth`. ckETH is burned from the ledger and `withdrawal_amount` is stored in `EthWithdrawalRequest`. [1](#0-0) 

**Phase 2 — Transaction creation:** `create_transaction` builds an EIP-1559 transaction with value `withdrawal_amount - max_tx_fee_estimate`. If fees are already too high at this point, the request is rescheduled (not reimbursed). [2](#0-1) 

**Phase 3 — Resubmission:** If the transaction is not mined, `SignedTransactionRequest::resubmit()` applies a strict ceiling check:

```rust
if new_tx_price.max_transaction_fee() > self.resubmission.allowed_max_transaction_fee() {
    return Err(ResubmitTransactionError::InsufficientTransactionFee { ... });
}
```

For ckETH withdrawals, `allowed_max_transaction_fee()` returns `withdrawal_amount` (the original burn amount). If gas fees spike such that `new_fee > withdrawal_amount`, this check fails. [3](#0-2) [4](#0-3) 

The error propagates to `create_resubmit_transactions`, which stops processing further transactions (blocking all higher-nonce withdrawals too): [5](#0-4) 

The caller `resubmit_transactions_batch` only logs the error — **no reimbursement is triggered**:

```rust
Err(e) => {
    log!(INFO, "Failed to resubmit transaction: {e:?}");
}
``` [6](#0-5) 

The withdrawal is now stuck because:
1. ckETH was already burned from the ledger (irreversible)
2. The transaction cannot be resubmitted (new fee exceeds `withdrawal_amount`)
3. The original low-fee transaction will not be mined (network fees are too high)
4. No automatic reimbursement path exists for this error variant

This contrasts with the finalized-transaction failure path, which does trigger reimbursement: [7](#0-6) 

The `create_transactions_batch` path (Phase 2) handles `InsufficientTransactionFee` by rescheduling the request (safe, since no burn has occurred yet). The resubmission path (Phase 3) has no equivalent safe fallback. [8](#0-7) 

---

### Impact Explanation

A user's ckETH is burned from the ICRC-1 ledger but the corresponding ETH is never delivered. The chain-fusion conservation invariant (1 ckETH ↔ 1 ETH) is violated for the affected withdrawal. Additionally, because `create_resubmit_transactions` stops at the first `InsufficientTransactionFee` error, **all subsequent pending withdrawals with higher nonces are also blocked** until the stuck transaction is resolved via a governance canister upgrade. [9](#0-8) 

---

### Likelihood Explanation

Ethereum gas fees have historically reached 300–500 gwei during high-activity periods (NFT mints, DeFi events, network congestion). The minimum ckETH withdrawal amount was recently reduced from 0.03 ETH to **0.005 ETH**: [10](#0-9) 

For a simple ETH transfer (21,000 gas), a gas price of ~238 gwei produces a fee of 0.005 ETH — exactly the new minimum. Any withdrawal at or near the minimum amount is vulnerable to a gas spike of this magnitude. The ckBTC minter already experienced a real-world instance of stuck transactions requiring an emergency upgrade due to a related fee-estimation issue: [11](#0-10) 

The attacker-controlled entry path is simply calling `withdraw_eth` with the minimum amount — no privileged access required. The gas spike is an external network condition, not an attacker action, but it is a realistic and historically observed trigger.

---

### Recommendation

When `ResubmitTransactionError::InsufficientTransactionFee` is encountered in `resubmit_transactions_batch`, the minter should initiate a reimbursement of the burned ckETH (minus any fees already consumed by the original transaction attempt), analogous to the reimbursement path used for finalized-but-failed Ethereum transactions. Alternatively, allow users to specify a maximum acceptable fee at withdrawal time, so the minter can cancel and reimburse if fees exceed the user's tolerance — directly analogous to the slippage parameter recommended in the Superform report.

---

### Proof of Concept

1. Alice calls `withdraw_eth` with `amount = 5_000_000_000_000_000` wei (0.005 ETH, the new minimum). The minter burns 0.005 ckETH from Alice's ledger account.
2. The minter estimates gas at 10 gwei and creates a transaction with value `0.005 ETH - 0.00021 ETH = 0.00479 ETH`. The transaction is sent to Ethereum.
3. The transaction is not mined (e.g., the fee estimate was too low).
4. Ethereum gas fees spike to 300 gwei (historically observed).
5. On the next timer tick, `resubmit_transactions_batch` calls `create_resubmit_transactions`. For Alice's transaction, `new_tx_price.max_transaction_fee() = 21_000 * 300 gwei = 0.0063 ETH > 0.005 ETH = allowed_max_transaction_fee`.
6. `InsufficientTransactionFee` is returned and only logged. No reimbursement event is emitted.
7. All subsequent pending withdrawals with higher nonces are also blocked.
8. Alice's 0.005 ckETH is permanently burned; she receives no ETH and has no recourse without a governance upgrade. [12](#0-11) [13](#0-12)

### Citations

**File:** rs/ethereum/cketh/minter/src/main.rs (L301-315)
```rust
    match client
        .burn_from(
            Account {
                owner: caller,
                subaccount: from_subaccount,
            },
            amount,
            BurnMemo::Convert {
                to_address: destination,
            },
        )
        .await
    {
        Ok(ledger_burn_index) => {
            let withdrawal_request = EthWithdrawalRequest {
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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1122-1134)
```rust
        WithdrawalRequest::CkEth(request) => {
            let transaction_price = gas_fee_estimate.to_price(gas_limit);
            let max_transaction_fee = transaction_price.max_transaction_fee();
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
```

**File:** rs/ethereum/cketh/minter/src/tx.rs (L136-144)
```rust
impl ResubmissionStrategy {
    pub fn allowed_max_transaction_fee(&self) -> Wei {
        match self {
            ResubmissionStrategy::ReduceEthAmount { withdrawal_amount } => *withdrawal_amount,
            ResubmissionStrategy::GuaranteeEthAmount {
                allowed_max_transaction_fee,
            } => *allowed_max_transaction_fee,
        }
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

**File:** rs/ethereum/cketh/mainnet/minter_upgrade_2026_05_29.md (L21-24)
```markdown
* Reduce the minimum ETH withdrawal amount by a factor of 6, from 0.03 ETH (`30_000_000_000_000_000` wei) to 0.005 ETH (`5_000_000_000_000_000` wei) — approximately $10 at current prices. The reasoning is as follows:
    * The current minimum dates back to December 2023, when the ckETH minter was installed (see proposal [126171](https://dashboard.internetcomputer.org/proposal/126171)). At that time ETH traded in a similar USD range (around $2000), but Ethereum mainnet transaction fees were averaging $5–$10 per transaction ([source](https://bitinfocharts.com/comparison/ethereum-transactionfees.html#3y)).
    * Today, Ethereum mainnet fees are in the order of cents and rarely exceed $1.
    * As explained [here](https://github.com/dfinity/ic/blob/14382b5abb14b8e7de2bd4a3fb402ba069b82861/rs/ethereum/cketh/docs/cketh.adoc?plain=1#L208), an order-of-magnitude safety margin is preserved so the minter can always submit the transaction even when the Ethereum network is congested and one or more resubmissions are needed (each resubmission requires at least a 10% fee bump). With current Ethereum fees of ~$0.10–$1, a $10 minimum still preserves the ~10× safety margin even after several fee bumps.
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
