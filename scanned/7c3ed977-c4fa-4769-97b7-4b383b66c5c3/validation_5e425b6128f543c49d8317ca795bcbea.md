### Title
Missing Reimbursement for `AmountTooLow` and `DustOutput` Withdrawal Failures - (`rs/bitcoin/ckbtc/minter/src/lib.rs`)

### Summary

The ckBTC minter permanently destroys user funds when a pending `retrieve_btc` withdrawal request is dropped with `BuildTxError::AmountTooLow` or `BuildTxError::DustOutput`. The user's ckBTC is burned at request-acceptance time, but if Bitcoin network fees spike before the minter processes the batch, the request is silently finalized as `AmountTooLow` with no reimbursement — a direct, irrecoverable loss of principal.

### Finding Description

When a user calls `retrieve_btc` or `retrieve_btc_with_approval`, the minter immediately burns the user's ckBTC on the ledger and enqueues a `RetrieveBtcRequest` in `pending_retrieve_btc_requests`. The burn is irreversible. The minter later calls `submit_pending_requests`, which attempts to build a Bitcoin transaction for the batch.

Three error paths exist in `submit_pending_requests`:

**Path 1 — `BuildTxError::InvalidTransaction` (lines 400–411):** calls `reimburse_canceled_requests`, which schedules a ckBTC mint back to the user's `reimbursement_account` minus a small fee. The user is made whole.

**Path 2 — `BuildTxError::AmountTooLow` (lines 412–434):** calls `state::audit::remove_retrieve_btc_request` with `FinalizedStatus::AmountTooLow`. No reimbursement is scheduled. The user's burned ckBTC is gone.

**Path 3 — `BuildTxError::DustOutput` (lines 436–453):** same as Path 2 for the offending request. No reimbursement.

`remove_retrieve_btc_request` only records an event and pushes a `FinalizedBtcRequest`; it never calls `reimburse_withdrawal` or `schedule_withdrawal_reimbursement`. [1](#0-0) [2](#0-1) [3](#0-2) 

The contrast with the `InvalidTransaction` path, which does reimburse, confirms this is an inconsistency rather than a design choice: [4](#0-3) [5](#0-4) 

The `reimbursement_account` field is populated for all modern `retrieve_btc` and `retrieve_btc_with_approval` calls, so the reimbursement infrastructure is present and functional — it is simply not invoked for these two error paths. [6](#0-5) [7](#0-6) 

### Impact Explanation

A user who calls `retrieve_btc` with an amount that passes the `fee_based_retrieve_btc_min_amount` check at submission time can permanently lose their entire withdrawal amount if Bitcoin network fees spike before the minter processes the batch. The ckBTC is burned, no BTC is sent, and no ckBTC is minted back. The `RetrieveBtcStatusV2::AmountTooLow` status is returned to the user with no recourse. This is a direct, irrecoverable loss of user principal — a chain-fusion burn without a corresponding mint or BTC transfer. [8](#0-7) 

### Likelihood Explanation

Bitcoin transaction fees are volatile and can spike by orders of magnitude within minutes (e.g., during inscription/ordinal demand surges). The minter queues requests for up to `max_time_in_queue_nanos` before processing them. Any user whose request sits in the queue during a fee spike is at risk. The entry path requires only a standard unprivileged call to `retrieve_btc` or `retrieve_btc_with_approval` — no special privileges or coordination needed. The real-world precedent of stuck ckBTC withdrawals (documented in `minter_upgrade_2025_06_27.md`) confirms that fee-related stuck transactions are not hypothetical. [9](#0-8) [10](#0-9) 

### Recommendation

Apply the same reimbursement logic used for `BuildTxError::InvalidTransaction` to the `AmountTooLow` and `DustOutput` error paths. Specifically, instead of calling `remove_retrieve_btc_request`, call `reimburse_canceled_requests` (or an equivalent that schedules a `ReimburseWithdrawalTask`) for each affected request. A suitable `WithdrawalReimbursementReason` variant (e.g., `FeesTooHigh`) should be added. The fee deducted for reimbursement in these cases can be zero or a nominal amount, since no Bitcoin transaction was broadcast.

### Proof of Concept

1. User calls `retrieve_btc_with_approval` with `amount = fee_based_retrieve_btc_min_amount` (passes the upfront check). ckBTC is burned; `reimbursement_account` is set.
2. Bitcoin network fees spike before `submit_pending_requests` runs.
3. `build_unsigned_transaction` returns `BuildTxError::AmountTooLow` because the fee now exceeds the withdrawal amount.
4. The minter executes the `AmountTooLow` branch: `remove_retrieve_btc_request` is called, finalizing the request with `FinalizedStatus::AmountTooLow`. No `schedule_withdrawal_reimbursement` is called.
5. User queries `retrieve_btc_status_v2` and receives `RetrieveBtcStatusV2::AmountTooLow`. Their ckBTC is gone with no path to recovery. [11](#0-10) [12](#0-11)

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

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L321-331)
```rust
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

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L302-314)
```rust
impl From<RetrieveBtcStatus> for RetrieveBtcStatusV2 {
    fn from(status: RetrieveBtcStatus) -> Self {
        match status {
            RetrieveBtcStatus::Unknown => RetrieveBtcStatusV2::Unknown,
            RetrieveBtcStatus::Pending => RetrieveBtcStatusV2::Pending,
            RetrieveBtcStatus::Signing => RetrieveBtcStatusV2::Signing,
            RetrieveBtcStatus::Sending { txid } => RetrieveBtcStatusV2::Sending { txid },
            RetrieveBtcStatus::Submitted { txid } => RetrieveBtcStatusV2::Submitted { txid },
            RetrieveBtcStatus::AmountTooLow => RetrieveBtcStatusV2::AmountTooLow,
            RetrieveBtcStatus::Confirmed { txid } => RetrieveBtcStatusV2::Confirmed { txid },
        }
    }
}
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L440-443)
```rust
    /// Maximum time of nanoseconds that a transaction should spend in the queue
    /// before being sent.
    pub max_time_in_queue_nanos: u64,

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
