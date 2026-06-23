### Title
Missing Reimbursement for `AmountTooLow` Finalized Withdrawal Requests Causes Permanent Loss of User ckBTC - (`rs/bitcoin/ckbtc/minter/src/lib.rs`)

### Summary

The ckBTC minter burns a user's ckBTC tokens when a withdrawal request is accepted. If Bitcoin network fees rise significantly after acceptance, the minter may be unable to build a valid Bitcoin transaction because the withdrawal amount no longer covers fees. In that case the request is silently finalized with `FinalizedStatus::AmountTooLow` and the user receives neither BTC nor a ckBTC reimbursement. This is structurally analogous to H-01: a normal system operation leaves user assets permanently unrecoverable because the code path that should return them is absent.

### Finding Description

The ckBTC withdrawal flow has two distinct failure modes that both occur *after* the user's ckBTC has already been burned:

**`BuildTxError::InvalidTransaction` (e.g., `TooManyInputs`)** — the minter calls `reimburse_canceled_requests`, which schedules a `ScheduleWithdrawalReimbursement` event and eventually mints ckBTC back to the user. [1](#0-0) 

**`BuildTxError::AmountTooLow`** — the minter calls `remove_retrieve_btc_request` with `FinalizedStatus::AmountTooLow`. No reimbursement is scheduled. The burned ckBTC is gone. [2](#0-1) 

The same missing reimbursement applies to the `BuildTxError::DustOutput` branch, which also finalizes with `FinalizedStatus::AmountTooLow`: [3](#0-2) 

`remove_retrieve_btc_request` only records the finalized status; it never enqueues a reimbursement: [4](#0-3) 

The `FinalizedStatus::AmountTooLow` variant has no associated reimbursement path in the state machine: [5](#0-4) 

The `retrieve_btc_status_v2` query exposes this as a terminal `AmountTooLow` status with no `WillReimburse` or `Reimbursed` successor: [6](#0-5) 

The reimbursement machinery that *does* exist (`schedule_withdrawal_reimbursement`, `reimburse_withdrawals`) is never invoked for this case: [7](#0-6) 

### Impact Explanation

A user who submits a valid withdrawal (amount ≥ `retrieve_btc_min_amount` at submission time) has their ckBTC burned immediately. If Bitcoin network fees spike before the minter batches the request into a transaction, the amount may fall below the fee threshold. The minter then discards the request with no on-chain reimbursement. The user permanently loses their ckBTC with no BTC received and no recovery path available through any public endpoint.

### Likelihood Explanation

Bitcoin fee spikes are a recurring, unpredictable, and externally-driven event. The minimum withdrawal amount is set at canister initialization/upgrade time and does not dynamically track the current fee market. During periods of high mempool congestion (e.g., Ordinals/Runes activity), fees can increase by an order of magnitude within hours, making previously-valid withdrawal amounts insufficient. Any user whose request sits in the pending queue across such a spike is at risk. This is a normal operational scenario, not a contrived edge case.

### Recommendation

Apply the same reimbursement pattern used for `BuildTxError::InvalidTransaction` to the `AmountTooLow` and `DustOutput` branches. Specifically, call `reimburse_canceled_requests` (or an equivalent) with a `WithdrawalReimbursementReason` variant for fee-related failures, so that `schedule_withdrawal_reimbursement` is recorded and the periodic `reimburse_withdrawals` task mints ckBTC back to the user's `reimbursement_account`.

### Proof of Concept

1. User calls `retrieve_btc_with_approval` with amount `X` where `X >= retrieve_btc_min_amount`. ckBTC tokens are burned immediately.
2. The request enters `pending_retrieve_btc_requests`.
3. Bitcoin network fees spike. The minter's fee estimator now computes `fee > X`.
4. `submit_pending_requests` is called. `build_unsigned_transaction` returns `BuildTxError::AmountTooLow`.
5. The minter executes the `AmountTooLow` branch: `remove_retrieve_btc_request(s, request, FinalizedStatus::AmountTooLow, runtime)` — no reimbursement is scheduled.
6. `retrieve_btc_status_v2(block_index)` returns `RetrieveBtcStatusV2::AmountTooLow` permanently.
7. The user's ckBTC is burned; no BTC was sent; no ckBTC is minted back. Funds are permanently lost. [2](#0-1) [4](#0-3)

### Citations

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

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L436-468)
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

**File:** rs/bitcoin/ckbtc/minter/src/reimbursement/mod.rs (L58-116)
```rust
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
