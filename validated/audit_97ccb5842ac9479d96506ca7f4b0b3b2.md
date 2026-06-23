### Title
ckETH Withdrawal Request Permanently Stuck in Pending Queue When Gas Fees Rise Above Withdrawal Amount — (`File: rs/ethereum/cketh/minter/src/withdraw.rs`)

---

### Summary

When a ckETH withdrawal request is accepted and burned on the ledger, but Ethereum gas fees subsequently rise above the withdrawal amount, the minter silently reschedules the request to the back of the pending queue indefinitely — with no reimbursement, no cancellation, and no user notification. The user's ckETH is permanently locked in the minter's pending queue.

---

### Finding Description

The ckETH minter's `withdraw_eth` endpoint accepts a withdrawal if `amount >= cketh_minimum_withdrawal_amount`. The minimum is set conservatively to ensure the amount covers gas fees at the time of the call. However, the actual Ethereum transaction is created asynchronously by a background timer task (`create_transactions_batch`).

In `create_transactions_batch`, for each pending withdrawal request, `create_transaction` is called. If the current gas fee estimate exceeds the withdrawal amount, `create_transaction` returns `CreateTransactionError::InsufficientTransactionFee`. The handler in `create_transactions_batch` responds by calling `reschedule_withdrawal_request`, which moves the request to the back of the pending queue:

```rust
Err(CreateTransactionError::InsufficientTransactionFee { .. }) => {
    log!(...);
    mutate_state(|s| s.eth_transactions.reschedule_withdrawal_request(request));
}
```

This loop repeats on every timer tick. There is **no upper bound on retries**, **no reimbursement path**, and **no cancellation mechanism** for a ckETH withdrawal that has already been burned on the ledger but cannot be converted into an Ethereum transaction because gas fees are too high.

The `WithdrawalError::AmountTooLow` check at ingress time uses `cketh_minimum_withdrawal_amount`, which is a static governance-set value. It does not account for real-time gas fee spikes. The `validate_config` check only ensures `minimum_withdrawal_amount > 0` and `> ledger_transfer_fee`, not that it is sufficient to cover actual Ethereum gas at any future point.

The analogous Etherlink bug caused a block failure when a tiny-value withdrawal was processed; the IC analog causes a withdrawal to be accepted (ckETH burned), then permanently stuck in the pending queue when gas fees spike above the withdrawal amount — a ledger conservation violation where burned ckETH is never redeemed as ETH and never reimbursed.

---

### Impact Explanation

- **Ledger conservation violation**: A user's ckETH is burned on the IC ledger (permanently reducing their balance), but the corresponding ETH is never sent and the ckETH is never reimbursed. The funds are effectively lost.
- **Permanent DoS on the withdrawal queue**: A stuck request occupies a slot in `pending_withdrawal_requests` indefinitely. The `has_pending_requests()` check keeps the timer running, consuming cycles. With enough such requests, the queue grows unboundedly.
- **No recovery path**: Unlike `BuildTxError::AmountTooLow` in ckBTC (which finalizes the request as `FinalizedStatus::AmountTooLow`), the ckETH minter has no equivalent finalization for this case — it only reschedules.

---

### Likelihood Explanation

This is a realistic scenario:

1. A user calls `withdraw_eth` with an amount just above `cketh_minimum_withdrawal_amount` (e.g., `5_000_000_000_000_000` wei = 0.005 ETH, the current minimum after the May 2026 upgrade).
2. Ethereum gas fees spike (e.g., during network congestion), causing `max_transaction_fee` to exceed the withdrawal amount.
3. The minter's background task repeatedly reschedules the request without ever resolving it.

The minimum was recently reduced by a factor of 6 (from 0.03 ETH to 0.005 ETH), making this scenario more likely during gas spikes. The governance proposal itself acknowledges the ~10× safety margin is needed for resubmissions.

---

### Recommendation

In `create_transactions_batch`, when `CreateTransactionError::InsufficientTransactionFee` is returned for a ckETH withdrawal, the minter should:

1. **Finalize the request** as unprocessable (analogous to ckBTC's `FinalizedStatus::AmountTooLow`).
2. **Reimburse the user** by minting back the burned ckETH amount (minus the ledger transfer fee), similar to how failed ckERC20 withdrawals are reimbursed via `FailedErc20WithdrawalRequest`.
3. Alternatively, implement a **maximum retry count** or **timeout** after which the request is cancelled and reimbursed.

---

### Proof of Concept

**Entry path** (unprivileged ingress sender):

1. User calls `withdraw_eth` with `amount = 5_000_000_000_000_000` wei (just above `cketh_minimum_withdrawal_amount`).
2. Minter passes the `amount < minimum_withdrawal_amount` check and burns ckETH on the ledger.
3. Ethereum gas fees spike so that `max_transaction_fee > 5_000_000_000_000_000` wei.
4. Background timer calls `create_transactions_batch` → `create_transaction` → returns `CreateTransactionError::InsufficientTransactionFee`.
5. Handler calls `reschedule_withdrawal_request` — request goes to back of queue.
6. Steps 4–5 repeat indefinitely on every timer tick.
7. User's ckETH is burned and never returned.

**Relevant code references:**

The ingress check that does not account for future gas spikes: [1](#0-0) 

The background task that silently reschedules instead of reimbursing: [2](#0-1) 

The `create_transaction` function that returns the error when amount < fee: [3](#0-2) 

The `reschedule_withdrawal_request` that moves the request to the back of the queue with no limit: [4](#0-3) 

The `panic!` in `WithdrawalError::from(LedgerBurnError)` that confirms the minter assumes `AmountTooLow` from the ledger is impossible — but does not handle the analogous case from the gas fee estimator: [5](#0-4) 

The ckBTC minter's correct handling (for contrast) — it finalizes as `AmountTooLow` with no reimbursement but at least does not loop: [6](#0-5)

### Citations

**File:** rs/ethereum/cketh/minter/src/main.rs (L291-296)
```rust
    let minimum_withdrawal_amount = read_state(|s| s.cketh_minimum_withdrawal_amount);
    if amount < minimum_withdrawal_amount {
        return Err(WithdrawalError::AmountTooLow {
            min_withdrawal_amount: minimum_withdrawal_amount.into(),
        });
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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L469-483)
```rust
    /// Move an existing withdrawal request to the back of the queue.
    pub fn reschedule_withdrawal_request<R: Into<WithdrawalRequest>>(&mut self, request: R) {
        let request = request.into();
        assert_eq!(
            self.pending_withdrawal_requests
                .iter()
                .filter(|r| r.cketh_ledger_burn_index() == request.cketh_ledger_burn_index())
                .count(),
            1,
            "BUG: expected exactly one withdrawal request with ckETH ledger burn index {}",
            request.cketh_ledger_burn_index()
        );
        self.remove_withdrawal_request(&request);
        self.record_withdrawal_request(request);
    }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1125-1134)
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
```

**File:** rs/ethereum/cketh/minter/src/endpoints.rs (L236-244)
```rust
            LedgerBurnError::AmountTooLow {
                minimum_burn_amount,
                failed_burn_amount,
                ledger,
            } => {
                panic!(
                    "BUG: withdrawal amount {failed_burn_amount} on the ckETH ledger {ledger:?} should always be higher than the ledger transaction fee {minimum_burn_amount}"
                )
            }
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L412-434)
```rust
            Err(BuildTxError::AmountTooLow) => {
                log!(
                    Priority::Info,
                    "[submit_pending_requests]: dropping requests for total BTC amount {} to addresses {} (too low to cover the fees)",
                    tx::DisplayAmount(batch.iter().map(|req| req.amount).sum::<u64>()),
                    batch
                        .iter()
                        .map(|req| req.address.display(s.btc_network))
                        .collect::<Vec<_>>()
                        .join(",")
                );

                // There is no point in retrying the request because the
                // amount is too low.
                for request in batch {
                    state::audit::remove_retrieve_btc_request(
                        s,
                        request,
                        state::FinalizedStatus::AmountTooLow,
                        runtime,
                    );
                }
                None
```
