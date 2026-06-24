### Title
ckETH Minter `pending_withdrawal_requests` Has No Expiration, Enabling Indefinite Cycles Drain via Rescheduled Failing Withdrawals - (`rs/ethereum/cketh/minter/src/withdraw.rs`)

---

### Summary

The ckETH minter's `pending_withdrawal_requests` queue has no expiration or retry-limit mechanism. When a withdrawal request fails with `CreateTransactionError::InsufficientTransactionFee` during `create_transactions_batch`, it is silently rescheduled to the back of the queue via `reschedule_withdrawal_request` and retried on every subsequent timer tick — indefinitely. This is the IC analog of M-03: a cross-chain execution attempt that fails does not advance the request to a terminal state, so the off-chain component (the minter's own timer) retries it forever, draining cycles and leaving user funds permanently locked.

---

### Finding Description

In `rs/ethereum/cketh/minter/src/withdraw.rs`, `create_transactions_batch` iterates over `withdrawal_requests_batch` and calls `create_transaction`. When the withdrawal amount is insufficient to cover the current Ethereum gas fee, the error branch is:

```rust
Err(CreateTransactionError::InsufficientTransactionFee { .. }) => {
    log!(INFO, "...Request moved back to end of queue.");
    mutate_state(|s| s.eth_transactions.reschedule_withdrawal_request(request));
}
``` [1](#0-0) 

`reschedule_withdrawal_request` simply removes the request from the front of `pending_withdrawal_requests` and appends it to the back — no attempt counter, no expiry timestamp, no terminal failure state:

```rust
pub fn reschedule_withdrawal_request<R: Into<WithdrawalRequest>>(&mut self, request: R) {
    self.remove_withdrawal_request(&request);
    self.record_withdrawal_request(request);
}
``` [2](#0-1) 

`process_retrieve_eth_requests` then re-arms the timer as long as `has_pending_requests()` is true:

```rust
if read_state(|s| s.eth_transactions.has_pending_requests()) {
    ic_cdk_timers::set_timer(
        crate::PROCESS_ETH_RETRIEVE_TRANSACTIONS_RETRY_INTERVAL,
        async { process_retrieve_eth_requests().await },
    );
}
``` [3](#0-2) 

`EthWithdrawalRequest` carries a `created_at` timestamp, but it is only surfaced in the `oldest_incomplete_withdrawal_timestamp` metric — never used to expire or cancel a request: [4](#0-3) 

The minimum withdrawal amount is validated at submission time in `withdraw_eth`, but the gas-fee check is deferred to `create_transaction` at execution time. A request that was valid at submission can become permanently unexecutable if Ethereum base fees rise above the withdrawal amount: [5](#0-4) 

---

### Impact Explanation

1. **Minter cycles drain**: Every `PROCESS_ETH_RETRIEVE_TRANSACTIONS_RETRY_INTERVAL` the minter wakes, calls `lazy_refresh_gas_fee_estimate` (an EVM-RPC outcall consuming cycles), iterates the batch, fails again, and re-arms the timer. A queue of permanently-stuck requests causes unbounded cycles consumption from the minter canister.

2. **User funds permanently locked**: The ckETH burn occurs in `withdraw_eth` before the request is enqueued. There is no cancellation endpoint. If Ethereum fees remain above the withdrawal amount indefinitely, the user's ckETH is burned and no ETH is ever sent. The `TxFinalizedStatus::PendingReimbursement` path is only reached after a transaction is mined and fails on-chain — it is never reached for a request that never progresses past `Pending`.

3. **No distinguishable terminal state**: A request that has been rescheduled 10,000 times is indistinguishable from one that was just submitted. Operators and users cannot determine whether a `Pending` request is genuinely new or has been failing for months.

---

### Likelihood Explanation

- Any unprivileged IC principal can call `withdraw_eth` with the minimum allowed amount.
- Ethereum base fees are volatile; spikes of 10–100× are historically common during network congestion.
- The minimum withdrawal amount (`cketh_minimum_withdrawal_amount`) is set conservatively but cannot anticipate extreme fee spikes.
- The attack requires burning real ckETH, but the cost is bounded by the minimum withdrawal amount while the cycles drain is unbounded over time.
- The real-world ckBTC minter upgrade proposal (`rs/bitcoin/ckbtc/mainnet/minter_upgrade_2025_06_27.md`) demonstrates that stuck-transaction scenarios do occur in production chain-fusion minters. [6](#0-5) 

---

### Recommendation

1. **Add a maximum reschedule count or expiry deadline** to `EthWithdrawalRequest`. After N reschedules or after `created_at + MAX_PENDING_DURATION`, transition the request to a terminal `Expired` state and trigger the existing reimbursement path.

2. **Expose a cancellation endpoint** (governance-gated or user-callable with proof of ownership) that moves a long-pending request to the reimbursement queue.

3. **Bound the retry timer**: If all pending requests have been rescheduled at least once in the current cycle, back off the retry interval exponentially rather than firing at the fixed `PROCESS_ETH_RETRIEVE_TRANSACTIONS_RETRY_INTERVAL`.

---

### Proof of Concept

1. Attacker calls `withdraw_eth` with `amount = cketh_minimum_withdrawal_amount` (e.g., `30_000_000_000_000_000` Wei). ckETH is burned; `AcceptedEthWithdrawalRequest` event is recorded; request enters `pending_withdrawal_requests`.

2. Ethereum base fee spikes to a level where `max_transaction_fee > withdrawal_amount`. On the next timer tick, `create_transactions_batch` hits `CreateTransactionError::InsufficientTransactionFee` and calls `reschedule_withdrawal_request`. The request moves to the back of the queue. Status remains `Pending`.

3. The timer re-arms. On the next tick, `lazy_refresh_gas_fee_estimate` makes EVM-RPC outcalls (consuming cycles). The same failure recurs. The request is rescheduled again.

4. This loop continues indefinitely. The minter's cycle balance decreases on every iteration. The user's ckETH is burned with no recourse. The `oldest_incomplete_withdrawal_timestamp` metric grows without bound, but no automated remediation is triggered.

### Citations

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L184-189)
```rust
    if read_state(|s| s.eth_transactions.has_pending_requests()) {
        ic_cdk_timers::set_timer(
            crate::PROCESS_ETH_RETRIEVE_TRANSACTIONS_RETRY_INTERVAL,
            async { process_retrieve_eth_requests().await },
        );
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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1096-1101)
```rust
    pub fn oldest_incomplete_withdrawal_timestamp(&self) -> Option<u64> {
        self.withdrawal_requests_iter()
            .chain(self.maybe_reimburse_requests_iter())
            .flat_map(|req| req.created_at().into_iter())
            .min()
    }
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
