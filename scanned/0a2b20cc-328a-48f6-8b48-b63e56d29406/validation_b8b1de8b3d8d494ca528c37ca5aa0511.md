### Title
ckERC20 Withdrawal Permanently Stuck and Minter Queue Blocked When Ethereum Gas Price Rises Above Locked `max_transaction_fee` - (File: rs/ethereum/cketh/minter/src/withdraw.rs)

---

### Summary

The ckETH minter locks the `max_transaction_fee` for a ckERC20 withdrawal at the moment `withdraw_erc20` is called, based on a gas fee estimate that may be up to 60 seconds stale. If Ethereum gas prices rise significantly after this lock-in, two distinct failure modes occur: (1) the pending withdrawal is rescheduled indefinitely with no reimbursement path, permanently locking the user's burned ckETH and ckERC20 tokens; and (2) a sent transaction that cannot be resubmitted at the higher fee silently blocks all subsequent minter withdrawals.

---

### Finding Description

**Step 1 — Fee lock-in at withdrawal time**

In `withdraw_erc20` (`rs/ethereum/cketh/minter/src/main.rs`), the minter calls `estimate_erc20_transaction_fee()`, which internally calls `lazy_refresh_gas_fee_estimate()`. This function uses a cached estimate that is considered valid for up to 60 seconds (`MAX_AGE_NS = 60_000_000_000`): [1](#0-0) 

The resulting fee is immediately burned from the user's ckETH balance and stored as the immutable `max_transaction_fee` field of the `Erc20WithdrawalRequest`: [2](#0-1) [3](#0-2) 

**Step 2 — Failure mode A: pending request rescheduled forever**

In `create_transactions_batch`, when `create_transaction` detects that the current gas fee exceeds `max_transaction_fee`, it returns `InsufficientTransactionFee`. The handler only logs the event and moves the request to the back of the pending queue: [4](#0-3) 

The check inside `create_transaction` for ckERC20 requests: [5](#0-4) 

Because `max_transaction_fee` is fixed at burn time and never updated, if gas prices remain elevated the request cycles through the queue indefinitely. The user's ckETH fee and ckERC20 tokens are both already burned with no reimbursement event emitted for this scenario.

**Step 3 — Failure mode B: sent transaction blocks entire minter**

In `resubmit_transactions_batch`, when a transaction already in `sent_tx` cannot be resubmitted because the new required fee exceeds `allowed_max_transaction_fee`, the error is only logged: [6](#0-5) 

`create_resubmit_transactions` stops iteration at the first `InsufficientTransactionFee` error: [7](#0-6) 

This means a single ckERC20 withdrawal whose locked fee is exceeded by a gas spike blocks all subsequent pending transactions (with higher nonces) from being resubmitted, halting the entire minter withdrawal pipeline.

---

### Impact Explanation

**Impact: Medium**

- A user's ckETH fee and ckERC20 tokens are burned at `withdraw_erc20` time. If gas prices subsequently rise above the locked `max_transaction_fee` and remain elevated, the withdrawal is rescheduled indefinitely with no reimbursement path. The documentation explicitly states "Overcharged transaction fees are not reimbursed" but provides no recovery for the inverse case (insufficient fee).
- In the resubmission scenario, a single stuck ckERC20 withdrawal can block all subsequent minter withdrawals from being resubmitted, causing a liveness failure for all users with pending withdrawals. [8](#0-7) 

---

### Likelihood Explanation

**Likelihood: Medium**

Ethereum gas prices are highly volatile and can spike by 2–10× within minutes during periods of high network activity (NFT mints, DeFi liquidations, etc.). The 60-second cache window for the gas fee estimate, combined with the IC's inter-canister call latency and the asynchronous nature of the withdrawal pipeline, creates a realistic window for the locked fee to become insufficient. The `estimate_max_fee_per_gas` formula uses `2 * base_fee_per_gas + max_priority_fee_per_gas` as a safety margin, but this is insufficient during rapid fee spikes. [9](#0-8) 

---

### Recommendation

1. **Add a reimbursement path for permanently stuck pending ckERC20 withdrawals**: When a ckERC20 withdrawal request has been rescheduled more than a configurable number of times (or after a timeout), emit a `FailedErc20WithdrawalRequest` event to reimburse both the ckETH fee and the ckERC20 tokens to the user.

2. **Handle resubmission failure gracefully**: When `ResubmitTransactionError::InsufficientTransactionFee` occurs, instead of only logging, consider emitting a reimbursement event for the ckERC20 tokens and marking the ckETH fee as consumed, so the stuck transaction does not block subsequent nonces indefinitely.

3. **Increase the fee safety margin**: Apply a larger multiplier (e.g., 3× or 4× `base_fee_per_gas`) when estimating the `max_transaction_fee` at withdrawal time to reduce the probability of the locked fee becoming insufficient during resubmission.

---

### Proof of Concept

**Scenario (Failure Mode A — Permanent pending lock):**

1. Alice calls `withdraw_erc20` when Ethereum `base_fee_per_gas = 10 gwei`. The minter estimates `max_transaction_fee = 65_000 * (2*10 + 1.5) gwei = ~1,397,500 gwei` and burns this amount of ckETH from Alice. Her ckERC20 tokens are also burned.
2. Before the background task `create_transactions_batch` runs, Ethereum gas spikes to `base_fee_per_gas = 30 gwei`. The new `min_max_fee_per_gas = 31.5 gwei`, requiring `65_000 * 31.5 = 2,047,500 gwei` — exceeding Alice's locked `max_transaction_fee`.
3. `create_transaction` returns `InsufficientTransactionFee`. The request is moved to the back of the queue via `reschedule_withdrawal_request`.
4. This repeats on every processing cycle. Alice's ckETH and ckERC20 are permanently burned with no reimbursement.

**Scenario (Failure Mode B — Minter queue block):**

1. Bob's ckERC20 withdrawal is sent to Ethereum with `max_fee_per_gas` derived from his locked `max_transaction_fee`.
2. Gas prices spike above Bob's `allowed_max_transaction_fee`. `resubmit` returns `InsufficientTransactionFee`.
3. `resubmit_transactions_batch` logs the error and stops iterating. All subsequent pending withdrawals (Carol's, Dave's, etc.) with higher nonces cannot be resubmitted.
4. The minter's entire withdrawal pipeline is halted until gas prices drop below Bob's locked fee. [10](#0-9)

### Citations

**File:** rs/ethereum/cketh/minter/src/tx.rs (L517-528)
```rust
    pub fn checked_estimate_max_fee_per_gas(&self) -> Option<WeiPerGas> {
        self.base_fee_per_gas
            .checked_mul(2_u8)
            .and_then(|base_fee_estimate| {
                base_fee_estimate.checked_add(self.max_priority_fee_per_gas)
            })
    }

    pub fn estimate_max_fee_per_gas(&self) -> WeiPerGas {
        self.checked_estimate_max_fee_per_gas()
            .unwrap_or(WeiPerGas::MAX)
    }
```

**File:** rs/ethereum/cketh/minter/src/tx.rs (L610-681)
```rust
pub async fn lazy_refresh_gas_fee_estimate() -> Option<GasFeeEstimate> {
    const MAX_AGE_NS: u64 = 60_000_000_000_u64; //60 seconds

    async fn do_refresh() -> Option<GasFeeEstimate> {
        let _guard = match TimerGuard::new(TaskType::RefreshGasFeeEstimate) {
            Ok(guard) => guard,
            Err(e) => {
                log!(
                    DEBUG,
                    "[refresh_gas_fee_estimate]: Failed retrieving guard: {e:?}",
                );
                return None;
            }
        };

        let fee_history = match eth_fee_history().await {
            Ok(fee_history) => fee_history,
            Err(e) => {
                log!(
                    INFO,
                    "[refresh_gas_fee_estimate]: Failed retrieving fee history: {e:?}",
                );
                return None;
            }
        };

        let gas_fee_estimate = match estimate_transaction_fee(&fee_history) {
            Ok(estimate) => {
                mutate_state(|s| {
                    s.last_transaction_price_estimate =
                        Some((ic_cdk::api::time(), estimate.clone()));
                });
                estimate
            }
            Err(e) => {
                log!(
                    INFO,
                    "[refresh_gas_fee_estimate]: Failed estimating gas fee: {e:?}",
                );
                return None;
            }
        };
        log!(
            INFO,
            "[refresh_gas_fee_estimate]: Estimated transaction fee: {:?}",
            gas_fee_estimate,
        );
        Some(gas_fee_estimate)
    }

    async fn eth_fee_history() -> Result<FeeHistory, MultiCallError<FeeHistory>> {
        read_state(rpc_client)
            .fee_history((5_u8, BlockTag::Latest))
            .with_reward_percentiles(vec![20])
            .with_cycles(MIN_ATTACHED_CYCLES)
            .try_send()
            .await
            .reduce_with_strategy(StrictMajorityByKey::new(|fee_history: &FeeHistory| {
                Nat::from(fee_history.oldest_block.clone())
            }))
    }

    let now_ns = ic_cdk::api::time();
    match read_state(|s| s.last_transaction_price_estimate.clone()) {
        Some((last_estimate_timestamp_ns, estimate))
            if now_ns < last_estimate_timestamp_ns.saturating_add(MAX_AGE_NS) =>
        {
            Some(estimate)
        }
        _ => do_refresh().await,
    }
}
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L430-432)
```rust
    let erc20_tx_fee = estimate_erc20_transaction_fee().await.ok_or_else(|| {
        WithdrawErc20Error::TemporarilyUnavailable("Failed to retrieve current gas fee".to_string())
    })?;
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L480-481)
```rust
                    let withdrawal_request = Erc20WithdrawalRequest {
                        max_transaction_fee: erc20_tx_fee,
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L242-244)
```rust
            Err(e) => {
                log!(INFO, "Failed to resubmit transaction: {e:?}");
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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L618-631)
```rust
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
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1159-1168)
```rust
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

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L275-275)
```text
. The minter retrieves the receipt of the finalized transaction (as done currently by the ckETH minter) and will reimburse the ckERC20 tokens in case the transaction failed. Overcharged transaction fees are not reimbursed.
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/tests.rs (L1014-1030)
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
