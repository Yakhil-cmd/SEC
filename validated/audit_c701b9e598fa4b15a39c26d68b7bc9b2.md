### Title
ckBTC Minter Silently Drops `AmountTooLow` and `DustOutput` Withdrawal Requests Without Reimbursing Burned ckBTC - (`rs/bitcoin/ckbtc/minter/src/lib.rs`)

---

### Summary

When the ckBTC minter's batch processor encounters `BuildTxError::AmountTooLow` or `BuildTxError::DustOutput` during `submit_pending_requests`, it finalizes the affected withdrawal requests with `FinalizedStatus::AmountTooLow` and discards them — but **never reimburses the ckBTC that was already burned** from the user's ledger account. This is a ledger conservation bug: ckBTC supply is permanently reduced without any corresponding BTC being sent or ckBTC being returned.

---

### Finding Description

The ckBTC withdrawal flow burns ckBTC from the user's account at request-acceptance time, then queues the request for batch processing. Later, during `submit_pending_requests`, `build_unsigned_transaction` is called. If it returns `BuildTxError::AmountTooLow` (total batch value too low to cover Bitcoin fees) or `BuildTxError::DustOutput` (a single request produces a dust output), the affected requests are finalized with no reimbursement:

```rust
// rs/bitcoin/ckbtc/minter/src/lib.rs  lines 412-434
Err(BuildTxError::AmountTooLow) => {
    for request in batch {
        state::audit::remove_retrieve_btc_request(
            s,
            request,
            state::FinalizedStatus::AmountTooLow,  // ← burned ckBTC is gone
            runtime,
        );
    }
    None
}
``` [1](#0-0) 

And for `DustOutput`:

```rust
// rs/bitcoin/ckbtc/minter/src/lib.rs  lines 436-467
Err(BuildTxError::DustOutput { address, amount }) => {
    for request in batch {
        if request.address == address && request.amount == amount {
            state::audit::remove_retrieve_btc_request(
                s,
                request,
                state::FinalizedStatus::AmountTooLow,  // ← no reimbursement
                runtime,
            );
        } ...
    }
    None
}
``` [2](#0-1) 

Contrast this with the `BuildTxError::InvalidTransaction` branch, which correctly calls `reimburse_canceled_requests` to schedule a ckBTC mint back to the user:

```rust
Err(BuildTxError::InvalidTransaction(err)) => {
    let reason = reimbursement::WithdrawalReimbursementReason::InvalidTransaction(err);
    let reimbursement_fee = fee_estimator
        .reimbursement_fee_for_pending_withdrawal_requests(batch.len() as u64);
    reimburse_canceled_requests(s, batch, reason, reimbursement_fee, runtime);
    None
}
``` [3](#0-2) 

`reimburse_canceled_requests` schedules a `ScheduleWithdrawalReimbursement` event and calls `state::audit::reimburse_withdrawal`, which eventually mints ckBTC back to the user via `reimburse_withdrawals`: [4](#0-3) [5](#0-4) 

No such path exists for `AmountTooLow` or `DustOutput`. The `FinalizedStatus::AmountTooLow` state is terminal — the minter's status API returns `RetrieveBtcStatus::AmountTooLow` and no further action is taken: [6](#0-5) 

The `remove_retrieve_btc_request` audit function only records a `RemovedRetrieveBtcRequest` event and pushes to `finalized_requests` — it does not schedule any reimbursement: [7](#0-6) 

---

### Impact Explanation

Any user who successfully called `retrieve_btc` or `retrieve_btc_with_approval` (burning their ckBTC) and whose request is later dropped with `AmountTooLow` or `DustOutput` permanently loses their ckBTC. The ckBTC total supply is reduced without any BTC being sent and without any ckBTC being returned. This is a direct ledger conservation violation: `total_supply` decreases but no value is delivered. [8](#0-7) 

---

### Likelihood Explanation

Bitcoin transaction fees are volatile. A withdrawal request that passes the `retrieve_btc_min_amount` check at submission time can become uneconomical to process if fees spike before the batch is formed. This is a realistic, non-adversarial scenario that has occurred historically during Bitcoin fee spikes. No privileged access or special conditions are required — any ordinary user submitting a withdrawal is exposed to this risk. The `DustOutput` path is additionally reachable when a single request in a multi-request batch produces a dust output at the current fee rate.

---

### Recommendation

Apply the same reimbursement logic used for `BuildTxError::InvalidTransaction` to the `AmountTooLow` and `DustOutput` branches. When a request is finalized with `FinalizedStatus::AmountTooLow`, call `reimburse_canceled_requests` (or an equivalent) to schedule a ckBTC mint back to the `reimbursement_account`. A new `WithdrawalReimbursementReason::AmountTooLow` variant should be added to distinguish this case in the event log.

---

### Proof of Concept

1. User calls `retrieve_btc_with_approval` with amount `X` satoshis (above `retrieve_btc_min_amount`). ckBTC is burned from user's ledger account. Request enters `pending_retrieve_btc_requests`.
2. Bitcoin network fees spike significantly before the next heartbeat batch.
3. `submit_pending_requests` runs. `build_unsigned_transaction` returns `BuildTxError::AmountTooLow` because `X` is now insufficient to cover fees.
4. `state::audit::remove_retrieve_btc_request(s, request, FinalizedStatus::AmountTooLow, runtime)` is called — no reimbursement is scheduled.
5. User queries `retrieve_btc_status_v2(block_index)` → returns `RetrieveBtcStatusV2::AmountTooLow`.
6. User's ckBTC is permanently gone. No BTC was sent. No ckBTC was returned. The `total_supply` of ckBTC is permanently reduced by `X`. [9](#0-8) [7](#0-6)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L292-329)
```rust
fn reimburse_canceled_requests<R: CanisterRuntime>(
    state: &mut state::CkBtcMinterState,
    requests: BTreeSet<state::RetrieveBtcRequest>,
    reason: WithdrawalReimbursementReason,
    total_fee: u64,
    runtime: &R,
) {
    assert!(!requests.is_empty());
    let fees = distribute(total_fee, requests.len() as u64);
    // This assertion makes sure the fee is smaller than each request amount
    assert!(
        fees[0] <= state.retrieve_btc_min_amount,
        "BUG: fees {fees:?} for {} withdrawal requests are larger than `retrieve_btc_min_amount` {}",
        requests.len(),
        state.retrieve_btc_min_amount
    );
    for (request, fee) in requests.into_iter().zip(fees.into_iter()) {
        if let Some(account) = request.reimbursement_account {
            let amount = request.amount.saturating_sub(fee);
            if amount > 0 {
                state::audit::reimburse_withdrawal(
                    state,
                    request.block_index,
                    amount,
                    account,
                    reason.clone(),
                    runtime,
                );
            }
        } else {
            log!(
                Priority::Info,
                "[reimburse_canceled_requests]: account is not found for retrieve_btc request ({:?})",
                request
            );
        }
    }
}
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L400-411)
```rust
            Err(BuildTxError::InvalidTransaction(err)) => {
                log!(
                    Priority::Info,
                    "[submit_pending_requests]: error in building transaction ({:?})",
                    err
                );
                let reason = reimbursement::WithdrawalReimbursementReason::InvalidTransaction(err);
                let reimbursement_fee = fee_estimator
                    .reimbursement_fee_for_pending_withdrawal_requests(batch.len() as u64);
                reimburse_canceled_requests(s, batch, reason, reimbursement_fee, runtime);
                None
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

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L436-467)
```rust
            Err(BuildTxError::DustOutput { address, amount }) => {
                log!(
                    Priority::Info,
                    "[submit_pending_requests]: dropping a request for BTC amount {} to {} (too low to cover the fees)",
                    tx::DisplayAmount(amount),
                    address.display(s.btc_network)
                );

                let mut requests_to_put_back = BTreeSet::new();
                for request in batch {
                    if request.address == address && request.amount == amount {
                        // Finalize the request that we cannot fulfill.
                        state::audit::remove_retrieve_btc_request(
                            s,
                            request,
                            state::FinalizedStatus::AmountTooLow,
                            runtime,
                        );
                    } else {
                        // Keep the rest of the requests in the batch, we will
                        // try to build a new transaction on the next iteration.
                        requests_to_put_back.insert(request);
                    }
                }

                s.push_from_in_flight_to_pending_requests(
                    state::SubmittedWithdrawalRequests::ToConfirm {
                        requests: requests_to_put_back,
                    },
                );

                None
```

**File:** rs/bitcoin/ckbtc/minter/src/reimbursement/mod.rs (L57-116)
```rust
/// Reimburse withdrawals that were canceled.
pub async fn reimburse_withdrawals<R: CanisterRuntime>(runtime: &R) {
    if state::read_state(|s| s.pending_withdrawal_reimbursements.is_empty()) {
        return;
    }
    let pending_reimbursements = state::read_state(|s| s.pending_withdrawal_reimbursements.clone());
    let mut error_count = 0;
    for (burn_index, reimbursement) in pending_reimbursements {
        // Ensure that even if we were to panic in the callback, after having contacted the ledger to mint the tokens,
        // this reimbursement request will not be processed again.
        let prevent_double_minting_guard = scopeguard::guard(burn_index, |index| {
            state::mutate_state(|s| {
                state::audit::quarantine_withdrawal_reimbursement(s, index, runtime)
            });
        });
        let memo = MintMemo::ReimburseWithdrawal {
            withdrawal_id: burn_index,
        };
        match runtime
            .mint_ckbtc(
                reimbursement.amount,
                reimbursement.account,
                Memo::from(crate::memo::encode(&memo)),
            )
            .await
        {
            Ok(mint_index) => {
                log!(
                    Priority::Debug,
                    "[reimburse_withdrawals]: Successfully reimbursed {:?} at mint block index {}",
                    reimbursement,
                    mint_index
                );
                state::mutate_state(|s| {
                    state::audit::reimburse_withdrawal_completed(s, burn_index, mint_index, runtime)
                });
            }
            Err(err) => {
                log!(
                    Priority::Info,
                    "[reimburse_withdrawals]: Failed to reimburse {:?}: {:?}. Will retry later",
                    reimbursement,
                    err
                );
                error_count += 1;
            }
        }
        // Defuse the guard. Note that in case of a panic in the callback (either before or after this point)
        // the defuse will not be effective (due to state rollback), and the guard that was
        // setup before the `mint_ckbtc` async call will be invoked.
        scopeguard::ScopeGuard::into_inner(prevent_double_minting_guard);
    }

    if error_count > 0 {
        log!(
            Priority::Info,
            "[reimburse_withdrawals] Failed to reimburse {error_count} withdrawal requests, retrying later."
        );
    }
}
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L259-267)
```rust
pub enum FinalizedStatus {
    /// The request amount was to low to cover the fees.
    AmountTooLow,
    /// The transaction that retrieves BTC got enough confirmations.
    Confirmed {
        /// The witness transaction identifier of the transaction.
        txid: Txid,
    },
}
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L907-916)
```rust
        match self
            .finalized_requests
            .iter()
            .find(|finalized_request| finalized_request.request.block_index() == block_index)
            .map(|final_req| final_req.state.clone())
        {
            Some(FinalizedStatus::AmountTooLow) => RetrieveBtcStatus::AmountTooLow,
            Some(FinalizedStatus::Confirmed { txid }) => RetrieveBtcStatus::Confirmed { txid },
            None => RetrieveBtcStatus::Unknown,
        }
```

**File:** rs/bitcoin/ckbtc/minter/src/state/audit.rs (L67-84)
```rust
pub fn remove_retrieve_btc_request<R: CanisterRuntime>(
    state: &mut CkBtcMinterState,
    request: RetrieveBtcRequest,
    status: FinalizedStatus,
    runtime: &R,
) {
    record_event(
        EventType::RemovedRetrieveBtcRequest {
            block_index: request.block_index,
        },
        runtime,
    );

    state.push_finalized_request(FinalizedBtcRequest {
        request: request.into(),
        state: status,
    });
}
```

**File:** rs/ledger_suite/common/ledger_core/src/balances.rs (L132-143)
```rust
    pub fn burn(
        &mut self,
        from: &S::AccountId,
        amount: S::Tokens,
    ) -> Result<(), BalanceError<S::Tokens>> {
        self.debit(from, amount.clone())?;
        self.token_pool = self
            .token_pool
            .checked_add(&amount)
            .expect("Overflow of the token pool while burning");
        Ok(())
    }
```
