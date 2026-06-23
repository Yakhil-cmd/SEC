### Title
Incomplete Cancellation of ckBTC Withdrawal Requests on `BuildTxError::AmountTooLow` and `BuildTxError::DustOutput` — (File: `rs/bitcoin/ckbtc/minter/src/lib.rs`)

### Summary

The ckBTC minter's `submit_pending_requests` function handles several `BuildTxError` variants when constructing a Bitcoin transaction for pending withdrawal requests. For `BuildTxError::InvalidTransaction`, the minter correctly calls `reimburse_canceled_requests`, which mints ckBTC back to the user minus a small fee. However, for `BuildTxError::AmountTooLow` and `BuildTxError::DustOutput`, the minter silently finalizes the affected requests with `FinalizedStatus::AmountTooLow` via `state::audit::remove_retrieve_btc_request` — **without minting any ckBTC back to the user**. The user's ckBTC was already burned at the time of the `retrieve_btc` call, so the funds are permanently lost.

### Finding Description

In `rs/bitcoin/ckbtc/minter/src/lib.rs`, the `submit_pending_requests` function processes a batch of pending `retrieve_btc` requests. When `build_unsigned_transaction` fails, the error is handled in a `match` block:

- **`BuildTxError::InvalidTransaction`** (lines 400–410): calls `reimburse_canceled_requests`, which schedules a ckBTC mint back to the user's `reimbursement_account` minus a fee. [1](#0-0) 

- **`BuildTxError::AmountTooLow`** (lines 412–435): iterates over the batch and calls `state::audit::remove_retrieve_btc_request(s, request, FinalizedStatus::AmountTooLow, runtime)` for each request. **No reimbursement is scheduled.** The user's ckBTC is permanently burned. [2](#0-1) 

- **`BuildTxError::DustOutput`** (lines 436–468): the single dust request is finalized with `FinalizedStatus::AmountTooLow` via `remove_retrieve_btc_request`. **No reimbursement is scheduled.** [3](#0-2) 

The `retrieve_btc` and `retrieve_btc_with_approval` flows burn the user's ckBTC **before** the request is queued:

```rust
let block_index =
    burn_ckbtcs(caller, args.amount, crate::memo::encode(&burn_memo).into()).await?;
``` [4](#0-3) 

The `reimbursement_account` field is set on the `RetrieveBtcRequest` at submission time: [5](#0-4) 

The `reimburse_canceled_requests` function (used for `InvalidTransaction`) correctly uses this field to mint ckBTC back: [6](#0-5) 

The `FinalizedStatus::AmountTooLow` state is a terminal state with no reimbursement path: [7](#0-6) 

### Impact Explanation

Any user who submits a `retrieve_btc` or `retrieve_btc_with_approval` request where the total batch amount (or a single request's amount) is too low to cover Bitcoin network fees at the time of processing will have their ckBTC permanently burned with no refund. This is a **ledger conservation bug**: ckBTC tokens are destroyed without a corresponding BTC transfer or ckBTC mint-back. The user loses their funds entirely. The `AmountTooLow` status is returned to the user as a terminal state with no recourse.

This is directly analogous to the reported Solidity bug: a cancellation/cleanup path removes state without returning the user's deposited collateral/fees.

### Likelihood Explanation

This is reachable by any unprivileged user of the ckBTC minter canister on mainnet. The trigger condition — Bitcoin network fees rising after a `retrieve_btc` request is accepted but before the batch is processed — is a realistic and recurring scenario given Bitcoin fee volatility. The `fee_based_retrieve_btc_min_amount` is updated dynamically, so a request accepted at a lower fee environment can become `AmountTooLow` when fees spike. The `DustOutput` variant can also be triggered when a batch of requests is assembled and one request's share of fees exceeds its amount.

### Recommendation

For `BuildTxError::AmountTooLow` and `BuildTxError::DustOutput`, apply the same reimbursement logic used for `BuildTxError::InvalidTransaction`: call `reimburse_canceled_requests` (or an equivalent) to schedule a ckBTC mint back to each affected request's `reimbursement_account`, minus a small penalty fee. The `FinalizedStatus` for these requests should be updated to reflect a reimbursed state rather than a silent `AmountTooLow` terminal state.

### Proof of Concept

1. User calls `retrieve_btc_with_approval(amount = X, address = ADDR)` where `X` is just above `fee_based_retrieve_btc_min_amount` at time T.
2. The minter burns `X` ckBTC from the user's account and queues a `RetrieveBtcRequest` with `reimbursement_account = Some(user_account)`.
3. Bitcoin network fees spike. At the next `submit_pending_requests` heartbeat, `build_unsigned_transaction` returns `BuildTxError::AmountTooLow` because `fee + minter_fee > X`.
4. The minter calls `remove_retrieve_btc_request(s, request, FinalizedStatus::AmountTooLow, runtime)` for the request — no reimbursement is scheduled.
5. `retrieve_btc_status_v2(block_index)` returns `RetrieveBtcStatusV2::AmountTooLow` — a terminal state.
6. The user's `X` ckBTC is permanently lost. No mint-back occurs. The `reimburse_canceled_requests` path (which would restore funds) is never invoked for this error variant.

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

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L400-410)
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
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L412-435)
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
            }
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

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L209-210)
```rust
    let block_index =
        burn_ckbtcs(caller, args.amount, crate::memo::encode(&burn_memo).into()).await?;
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L212-222)
```rust
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

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L258-267)
```rust
#[derive(Clone, Eq, PartialEq, Debug, Deserialize, Serialize)]
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
