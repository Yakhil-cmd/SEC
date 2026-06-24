### Title
ckBTC Minter Burns User ckBTC Without Reimbursement When Batch Fails With `AmountTooLow` - (`rs/bitcoin/ckbtc/minter/src/lib.rs`)

---

### Summary

When the ckBTC minter's `submit_pending_requests` function attempts to build a Bitcoin transaction for a batch of pending `retrieve_btc` requests and encounters `BuildTxError::AmountTooLow` or `BuildTxError::DustOutput`, it permanently removes the affected requests from state with `FinalizedStatus::AmountTooLow` **without reimbursing the users' already-burned ckBTC**. This is in direct contrast to the `BuildTxError::InvalidTransaction` path, which correctly calls `reimburse_canceled_requests`. The result is a permanent, unrecoverable loss of user funds.

---

### Finding Description

The ckBTC minter's `retrieve_btc` flow works as follows:

1. A user calls `retrieve_btc` (or `retrieve_btc_with_approval`).
2. The minter burns the user's ckBTC from the ICRC-1 ledger.
3. A `RetrieveBtcRequest` is accepted into the pending queue.
4. The background task `submit_pending_requests` periodically batches pending requests and attempts to build a Bitcoin transaction.

The vulnerability is in step 4. In `submit_pending_requests`, three distinct error branches exist when `build_unsigned_transaction` fails:

**`BuildTxError::InvalidTransaction` — correctly reimburses users:** [1](#0-0) 

```rust
Err(BuildTxError::InvalidTransaction(err)) => {
    let reason = reimbursement::WithdrawalReimbursementReason::InvalidTransaction(err);
    let reimbursement_fee = ...;
    reimburse_canceled_requests(s, batch, reason, reimbursement_fee, runtime);
    None
}
```

**`BuildTxError::AmountTooLow` — silently drops requests with NO reimbursement:** [2](#0-1) 

```rust
Err(BuildTxError::AmountTooLow) => {
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

**`BuildTxError::DustOutput` — same: removes the offending request with NO reimbursement:** [3](#0-2) 

The `FinalizedStatus::AmountTooLow` is a terminal state. Once a request is finalized with this status, there is no subsequent reimbursement path: [4](#0-3) 

The event log replay confirms this: `RemovedRetrieveBtcRequest` simply pushes to `finalized_requests` with `AmountTooLow` and no reimbursement is scheduled: [5](#0-4) 

The `retrieve_btc` endpoint validates the amount against `min_retrieve_amount` at submission time, but Bitcoin network fees are dynamic. A request that was valid at submission can become part of a batch whose **total** is insufficient to cover fees after a fee spike, triggering `AmountTooLow` at batch-build time — after the ckBTC has already been burned. [6](#0-5) 

---

### Impact Explanation

A user who calls `retrieve_btc` with a valid amount:
1. Has their ckBTC permanently burned from the ICRC-1 ledger.
2. Receives no BTC (the Bitcoin transaction is never sent).
3. Receives no ckBTC reimbursement (the request is finalized with `AmountTooLow`).
4. Has no recourse — the `RetrieveBtcStatusV2::AmountTooLow` is a terminal state with no recovery path.

This is a **ledger conservation bug**: ckBTC supply is reduced without a corresponding BTC being sent, and the user's funds are permanently destroyed. The `InvalidTransaction` path demonstrates the protocol team knows how to reimburse users; the `AmountTooLow` path simply omits it.

---

### Likelihood Explanation

The `BuildTxError::AmountTooLow` condition at batch-build time is reachable without any privileged access:

- Any user can call `retrieve_btc` with an amount above the current minimum.
- Bitcoin network fees are volatile. A fee spike between request submission and batch processing can cause the batch's total amount to be insufficient to cover fees.
- Multiple users submitting small-but-valid requests that are batched together can collectively trigger this condition.
- The condition is more likely during periods of high Bitcoin network congestion.

The entry path is fully unprivileged: `retrieve_btc` is a public endpoint callable by any principal. [7](#0-6) 

---

### Recommendation

The `BuildTxError::AmountTooLow` and `BuildTxError::DustOutput` branches in `submit_pending_requests` should call `reimburse_canceled_requests` (or an equivalent reimbursement mechanism) instead of silently finalizing requests with `FinalizedStatus::AmountTooLow`. This mirrors the existing correct behavior in the `BuildTxError::InvalidTransaction` branch.

---

### Proof of Concept

1. User calls `retrieve_btc` with `amount = min_retrieve_amount` (e.g., the current minimum). ckBTC is burned. Request enters pending queue.
2. Bitcoin network fees spike significantly.
3. `submit_pending_requests` runs. `build_unsigned_transaction` returns `BuildTxError::AmountTooLow` because the batch total (possibly just this one request) cannot cover the elevated fees.
4. `remove_retrieve_btc_request(s, request, FinalizedStatus::AmountTooLow, runtime)` is called.
5. User queries `retrieve_btc_status` and receives `RetrieveBtcStatusV2::AmountTooLow`.
6. User's ckBTC is gone. No BTC was sent. No reimbursement was issued.

The asymmetry with `InvalidTransaction` (which calls `reimburse_canceled_requests`) confirms this is an omission, not an intentional design choice. [8](#0-7)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L400-434)
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

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L436-453)
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
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L257-267)
```rust
/// The outcome of a retrieve_btc request.
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

**File:** rs/bitcoin/ckbtc/minter/src/state/eventlog.rs (L405-418)
```rust
                EventType::RemovedRetrieveBtcRequest { block_index } => {
                    let request = state
                    .remove_pending_retrieve_btc_request(block_index)
                    .ok_or_else(|| {
                        ReplayLogError::InconsistentLog(format!(
                            "Attempted to remove a non-pending retrieve_btc request {block_index}"
                        ))
                    })?;

                    state.push_finalized_request(FinalizedBtcRequest {
                        request: request.into(),
                        state: FinalizedStatus::AmountTooLow,
                    })
                }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L146-150)
```rust
pub async fn retrieve_btc<R: CanisterRuntime>(
    args: RetrieveBtcArgs,
    runtime: &R,
) -> Result<RetrieveBtcOk, RetrieveBtcError> {
    let caller = ic_cdk::api::msg_caller();
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L169-171)
```rust
    if args.amount < min_retrieve_amount {
        return Err(RetrieveBtcError::AmountTooLow(min_retrieve_amount));
    }
```
