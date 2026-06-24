Looking at the M-02 vulnerability class — **burn/pay more than received with no incentive mechanism** — I need to find an IC analog where tokens are burned but no equivalent value is returned, with no reimbursement path.

### Title
ckBTC Burned Without Reimbursement When `AmountTooLow` at Batch Submission Time — (`rs/bitcoin/ckbtc/minter/src/lib.rs`)

---

### Summary

The ckBTC minter burns the user's ckBTC at `retrieve_btc` call time, before any Bitcoin transaction is built. If Bitcoin network fees spike between request acceptance and batch processing, the minter may determine the withdrawal amount is too low to cover fees (`BuildTxError::AmountTooLow` or `BuildTxError::DustOutput`). In that case the request is silently finalized with `FinalizedStatus::AmountTooLow` and **no reimbursement is issued**, permanently destroying the user's ckBTC with zero BTC received. The `InvalidTransaction` error path, by contrast, explicitly calls `reimburse_canceled_requests`. The asymmetry is the root cause.

---

### Finding Description

**Step 1 — ckBTC is burned eagerly at request time.**

In `retrieve_btc`, the full withdrawal amount is burned from the caller's account before the request is even queued:

```rust
// rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs  lines 209-210
let block_index =
    burn_ckbtcs(caller, args.amount, crate::memo::encode(&burn_memo).into()).await?;
```

A `reimbursement_account` is stored in the request struct (lines 218-221), signalling that the design anticipated reimbursement paths.

**Step 2 — Batch processing may later reject the request as `AmountTooLow`.**

The minter's timer periodically calls `submit_pending_requests`. It re-estimates Bitcoin fees at that moment. If fees have risen since the request was accepted, `build_unsigned_transaction` returns `BuildTxError::AmountTooLow` or `BuildTxError::DustOutput`. Both branches finalize the request with no reimbursement:

```rust
// rs/bitcoin/ckbtc/minter/src/lib.rs  lines 412-434
Err(BuildTxError::AmountTooLow) => {
    // There is no point in retrying the request because the amount is too low.
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

```rust
// rs/bitcoin/ckbtc/minter/src/lib.rs  lines 436-467
Err(BuildTxError::DustOutput { address, amount }) => {
    // ...
    state::audit::remove_retrieve_btc_request(
        s,
        request,
        state::FinalizedStatus::AmountTooLow,  // same finalization, no reimbursement
        runtime,
    );
    // ...
}
```

**Step 3 — The `InvalidTransaction` path does reimburse, proving the mechanism exists.**

```rust
// rs/bitcoin/ckbtc/minter/src/lib.rs  lines 400-410
Err(BuildTxError::InvalidTransaction(err)) => {
    let reason = reimbursement::WithdrawalReimbursementReason::InvalidTransaction(err);
    let reimbursement_fee = fee_estimator
        .reimbursement_fee_for_pending_withdrawal_requests(batch.len() as u64);
    reimburse_canceled_requests(s, batch, reason, reimbursement_fee, runtime);
    None
}
```

The reimbursement infrastructure exists and is used for `InvalidTransaction`. It is simply never invoked for `AmountTooLow` or `DustOutput`.

---

### Impact Explanation

A user who calls `retrieve_btc` with an amount that passes the minimum-amount guard at call time can have their ckBTC permanently destroyed with zero BTC received if Bitcoin fees spike before the minter processes the batch. The ckBTC ledger records a burn; the Bitcoin network records nothing. The user has no recourse: the `reimbursement_account` field stored in the request is never consulted for this error path. This is a **ledger conservation violation** — ckBTC supply decreases without a corresponding BTC output, and the user suffers a total loss of the withdrawn amount.

---

### Likelihood Explanation

Bitcoin mempool fees are volatile. Historical fee spikes (e.g., Ordinals inscription waves, halving periods) have caused fees to increase by 10–100× within hours. The minter's `fee_based_retrieve_btc_min_amount` is updated from fee percentiles, but there is an unbounded window between when a user's ckBTC is burned and when the minter's timer next runs and builds the batch. Any fee spike in that window can push a previously-valid request into `AmountTooLow` territory. No privileged access is required; the trigger is ordinary Bitcoin network congestion.

---

### Recommendation

Apply the same reimbursement logic used for `InvalidTransaction` to the `AmountTooLow` and `DustOutput` branches. Concretely, replace the bare `remove_retrieve_btc_request` calls with `reimburse_canceled_requests`, passing a suitable `WithdrawalReimbursementReason` variant (e.g., `AmountTooLow`). The `reimbursement_account` is already stored in every `RetrieveBtcRequest`, so no structural change to the request type is needed. A small reimbursement fee (analogous to the one charged for `InvalidTransaction`) can be deducted to cover minter overhead.

---

### Proof of Concept

1. Bitcoin mainnet fees are low (e.g., 5 sat/vbyte). `fee_based_retrieve_btc_min_amount` is 100 000 sat.
2. User calls `retrieve_btc` with `amount = 110 000 sat`. The guard passes; `burn_ckbtcs` destroys 110 000 ckBTC from the user's account. The request enters the pending queue with `reimbursement_account = caller`.
3. Before the minter's next timer tick, a fee spike pushes the median fee to 800 sat/vbyte. The minter recomputes `fee_based_retrieve_btc_min_amount` → 500 000 sat.
4. `submit_pending_requests` calls `build_unsigned_transaction`. The total fee for a 1-input/2-output transaction at 800 sat/vbyte exceeds 110 000 sat → `BuildTxError::AmountTooLow`.
5. The `AmountTooLow` branch calls `remove_retrieve_btc_request(..., FinalizedStatus::AmountTooLow, ...)`. No call to `reimburse_canceled_requests`.
6. The user queries `retrieve_btc_status(block_index)` and receives `FinalizedStatus::AmountTooLow`. Their 110 000 ckBTC is gone; they received 0 BTC; no reimbursement is ever issued. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** rs/bitcoin/ckbtc/minter/src/fees/mod.rs (L128-147)
```rust
    /// Returns the minimum withdrawal amount based on the current median fee rate (in millisatoshi per byte).
    /// The returned amount is in satoshi.
    fn fee_based_minimum_withdrawal_amount(&self, median_fee_rate: FeeRate) -> Satoshi {
        match self.network {
            Network::Mainnet | Network::Testnet => {
                const PER_REQUEST_RBF_BOUND: u64 = 22_100;
                const PER_REQUEST_VSIZE_BOUND: u64 = 221;
                const PER_REQUEST_MINTER_FEE_BOUND: u64 = 305;

                ((PER_REQUEST_RBF_BOUND
                    + median_fee_rate.fee_ceil(PER_REQUEST_VSIZE_BOUND)
                    + PER_REQUEST_MINTER_FEE_BOUND
                    + self.check_fee)
                    / 50_000) //TODO DEFI-2187: adjust increment of minimum withdrawal amount to be a multiple of retrieve_btc_min_amount/2
                    * 50_000
                    + self.retrieve_btc_min_amount
            }
            Network::Regtest => self.retrieve_btc_min_amount,
        }
    }
```
