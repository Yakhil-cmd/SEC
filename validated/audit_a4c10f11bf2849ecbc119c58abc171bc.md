Audit Report

## Title
ckBTC Minter Permanently Burns User ckBTC Without Reimbursement on `AmountTooLow` and `DustOutput` Errors - (`rs/bitcoin/ckbtc/minter/src/lib.rs`)

## Summary

The ckBTC minter burns user ckBTC at withdrawal request acceptance time, then queues the request for batch processing. When `submit_pending_requests` encounters `BuildTxError::AmountTooLow` or `BuildTxError::DustOutput`, it finalizes the affected requests via `remove_retrieve_btc_request` with `FinalizedStatus::AmountTooLow` but never schedules a reimbursement mint. The burned ckBTC is permanently destroyed with no BTC sent and no ckBTC returned, violating ledger conservation.

## Finding Description

**Burn at acceptance time:** Both `retrieve_btc` and `retrieve_btc_with_approval` call `burn_ckbtcs` / `burn_ckbtcs_icrc2` before queuing the request. The ckBTC leaves the user's account at this point, not at transaction submission. [1](#0-0) [2](#0-1) 

Both requests also set `reimbursement_account`, meaning the reimbursement infrastructure is in place and would work if called. [3](#0-2) 

**Missing reimbursement in `AmountTooLow` branch:** When `build_unsigned_transaction` returns `BuildTxError::AmountTooLow`, the minter calls `remove_retrieve_btc_request` with `FinalizedStatus::AmountTooLow` for every request in the batch. No reimbursement is scheduled. [4](#0-3) 

**Missing reimbursement in `DustOutput` branch:** Similarly, the dust-producing request is finalized with `FinalizedStatus::AmountTooLow` and no reimbursement. [5](#0-4) 

**`remove_retrieve_btc_request` does not schedule reimbursement:** It only records a `RemovedRetrieveBtcRequest` event and pushes to `finalized_requests`. There is no call to `reimburse_withdrawal` or any equivalent. [6](#0-5) 

**Contrast with `InvalidTransaction` branch:** This branch correctly calls `reimburse_canceled_requests`, which calls `state::audit::reimburse_withdrawal` and enqueues a `ScheduleWithdrawalReimbursement` event that eventually mints ckBTC back to the user. [7](#0-6) [8](#0-7) 

**`WithdrawalReimbursementReason` has no `AmountTooLow` variant:** The reimbursement reason enum only contains `InvalidTransaction`, confirming no reimbursement path exists for the affected error cases. [9](#0-8) 

**Terminal state:** Once finalized as `AmountTooLow`, the status API returns `RetrieveBtcStatus::AmountTooLow` and no further processing occurs. [10](#0-9) 

## Impact Explanation

Any user whose withdrawal request is finalized with `FinalizedStatus::AmountTooLow` permanently loses their ckBTC. The ckBTC total supply is reduced without any BTC being sent and without any ckBTC being returned. This is a concrete, permanent loss of in-scope chain-key/ledger assets (ckBTC) with direct user harm. This matches the **High** impact category: "Significant Chain Fusion, ck-token, ledger security impact with concrete user or protocol harm." The loss scales with the number of affected requests and the amounts involved; during a fee spike affecting many queued withdrawals, aggregate losses could be substantial.

## Likelihood Explanation

Bitcoin transaction fees are volatile and have historically spiked dramatically. A withdrawal request that passes the `retrieve_btc_min_amount` check at submission time can become uneconomical before the next heartbeat batch runs. No privileged access, special conditions, or adversarial action is required — any ordinary user submitting a withdrawal during a period of rising fees is exposed. The `DustOutput` path is additionally reachable when a single request in a multi-request batch produces a dust output at the current fee rate. Both conditions are realistic, non-adversarial, and have occurred on mainnet.

## Recommendation

Apply the same reimbursement logic used for `BuildTxError::InvalidTransaction` to both the `AmountTooLow` and `DustOutput` branches in `submit_pending_requests` (`rs/bitcoin/ckbtc/minter/src/lib.rs`):

1. Add a new `WithdrawalReimbursementReason::AmountTooLow` variant to `rs/bitcoin/ckbtc/minter/src/reimbursement/mod.rs`.
2. In the `BuildTxError::AmountTooLow` branch, replace the `remove_retrieve_btc_request` loop with a call to `reimburse_canceled_requests(s, batch, WithdrawalReimbursementReason::AmountTooLow, 0, runtime)`.
3. In the `BuildTxError::DustOutput` branch, do the same for the dust-producing request before putting the remaining requests back into the pending queue.
4. Ensure `reimburse_canceled_requests` handles a zero fee correctly (the existing `assert!(fees[0] <= state.retrieve_btc_min_amount)` will pass with fee=0).

## Proof of Concept

1. User calls `retrieve_btc_with_approval` with amount `X` satoshis (above `retrieve_btc_min_amount`). `burn_ckbtcs_icrc2` is called — ckBTC is burned from the user's account. Request enters `pending_retrieve_btc_requests` with `reimbursement_account` set.
2. Bitcoin network fees spike before the next heartbeat.
3. `submit_pending_requests` runs. `build_unsigned_transaction` returns `BuildTxError::AmountTooLow`.
4. The `AmountTooLow` branch executes: `state::audit::remove_retrieve_btc_request(s, request, FinalizedStatus::AmountTooLow, runtime)` is called. No reimbursement is scheduled.
5. User queries `retrieve_btc_status_v2(block_index)` → returns `RetrieveBtcStatusV2::AmountTooLow`. State is terminal.
6. User's ckBTC is permanently gone. No BTC was sent. No ckBTC was returned.

A deterministic integration test can reproduce this by: (a) calling `retrieve_btc_with_approval`, (b) advancing the fee estimate above the withdrawal amount, (c) triggering `submit_pending_requests`, and (d) asserting that `retrieve_btc_status_v2` returns `AmountTooLow` while the ckBTC ledger total supply has decreased by `X` with no corresponding mint or BTC transaction.

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L209-222)
```rust
    let block_index =
        burn_ckbtcs(caller, args.amount, crate::memo::encode(&burn_memo).into()).await?;

    let request = RetrieveBtcRequest {
        amount: args.amount,
        address: parsed_address,
        block_index,
        received_at: ic_cdk::api::time(),
        kyt_provider: None,
        reimbursement_account: Some(Account {
            owner: caller,
            subaccount: None,
        }),
    };
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L314-333)
```rust
    let block_index = burn_ckbtcs_icrc2(
        caller_account,
        args.amount,
        crate::memo::encode(&burn_memo_icrc2).into(),
    )
    .await?;

    let request = RetrieveBtcRequest {
        amount: args.amount,
        address: parsed_address,
        block_index,
        received_at: ic_cdk::api::time(),
        kyt_provider: None,
        reimbursement_account: Some(Account {
            owner: caller,
            subaccount: args.from_subaccount,
        }),
    };

    mutate_state(|s| state::audit::accept_retrieve_btc_request(s, request, runtime));
```

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

**File:** rs/bitcoin/ckbtc/minter/src/reimbursement/mod.rs (L39-43)
```rust
#[derive(Clone, Eq, PartialEq, Debug, Deserialize, Serialize, candid::CandidType)]
pub enum WithdrawalReimbursementReason {
    #[serde(rename = "invalid_transaction")]
    InvalidTransaction(InvalidTransactionError),
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
