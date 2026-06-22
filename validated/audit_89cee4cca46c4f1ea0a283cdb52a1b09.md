### Title
ckBTC Minter Finalizes `AmountTooLow` Withdrawal Requests Without Reimbursement, Causing Permanent ckBTC Loss - (`File: rs/bitcoin/ckbtc/minter/src/lib.rs`)

### Summary

When a user calls `retrieve_btc` or `retrieve_btc_with_approval`, their ckBTC is burned immediately and atomically before the BTC withdrawal is queued. If Bitcoin network fees increase between request acceptance and transaction construction, the minter may finalize the request as `AmountTooLow` — permanently discarding the user's burned ckBTC with no reimbursement. This is a direct analog to M-11: a user's tokens are consumed in exchange for zero underlying value.

### Finding Description

**Step 1 — ckBTC is burned before the request is queued.**

In `retrieve_btc`, the burn happens at line 210 before `accept_retrieve_btc_request` is called:

```rust
let block_index =
    burn_ckbtcs(caller, args.amount, crate::memo::encode(&burn_memo).into()).await?;
// ...
mutate_state(|s| state::audit::accept_retrieve_btc_request(s, request, &IC_CANISTER_RUNTIME));
``` [1](#0-0) 

**Step 2 — The minter's timer processes pending requests and may encounter `AmountTooLow`.**

In `submit_pending_requests` (called from the timer), `build_tx` is invoked on a batch. If the current Bitcoin fee estimate has risen since the request was accepted, `BuildTxError::AmountTooLow` is returned. The handler finalizes every request in the batch as `FinalizedStatus::AmountTooLow` with **no reimbursement**:

```rust
Err(BuildTxError::AmountTooLow) => {
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
``` [2](#0-1) 

**Step 3 — Contrast with `InvalidTransaction`, which IS reimbursed.**

The immediately preceding arm for `BuildTxError::InvalidTransaction` calls `reimburse_canceled_requests`, crediting users back their burned ckBTC (minus a penalty fee):

```rust
Err(BuildTxError::InvalidTransaction(err)) => {
    let reason = reimbursement::WithdrawalReimbursementReason::InvalidTransaction(err);
    let reimbursement_fee = fee_estimator
        .reimbursement_fee_for_pending_withdrawal_requests(batch.len() as u64);
    reimburse_canceled_requests(s, batch, reason, reimbursement_fee, runtime);
    None
}
``` [3](#0-2) 

The `AmountTooLow` arm has no equivalent call. The `DustOutput` arm also finalizes individual requests as `AmountTooLow` without reimbursement. [4](#0-3) 

**Step 4 — The min-amount guard does not prevent this.**

The `fee_based_retrieve_btc_min_amount` check at request time only validates the amount against the fee estimate *at that moment*:

```rust
if args.amount < min_retrieve_amount {
    return Err(RetrieveBtcError::AmountTooLow(min_retrieve_amount));
}
``` [5](#0-4) 

Bitcoin fees are volatile. A request accepted at fee level F can become unprocessable if fees rise to F' > amount before the minter's timer fires. The `fee_based_retrieve_btc_min_amount` is updated dynamically but the already-queued request is not re-validated against the new minimum before being finalized. [6](#0-5) 

### Impact Explanation

A user who calls `retrieve_btc` with an amount that passes the minimum check at call time can permanently lose their ckBTC if Bitcoin fees spike before the minter processes the request. The ckBTC ledger burn is irreversible; the minter finalizes the request as `AmountTooLow` and records no reimbursement obligation. The user's `retrieve_btc_status_v2` will show `AmountTooLow` with no path to recovery. This is a direct loss of user funds — the chain-fusion analog of M-11's "burn tokens in exchange for zero underlying."

### Likelihood Explanation

Bitcoin fees are known to spike sharply (e.g., during inscription/ordinal activity). The minter processes requests in batches on a timer, so there is always a window between burn and BTC transaction construction. A user who submits a request just above the current minimum during a low-fee period is at risk if fees rise before the batch is processed. This is an unprivileged ingress path (`retrieve_btc` is a public update call) and requires no special access.

### Recommendation

Add a reimbursement path for `AmountTooLow`, mirroring the `InvalidTransaction` handler. When a request cannot be fulfilled because the amount is too low to cover fees, call `reimburse_canceled_requests` (or an equivalent) with a `WithdrawalReimbursementReason` variant for fee-too-high, deducting a small penalty fee to cover the cost of the failed attempt. This ensures users are never left with burned ckBTC and no BTC.

### Proof of Concept

1. Bitcoin fees are currently low; `fee_based_retrieve_btc_min_amount` = 5,000 sat.
2. User calls `retrieve_btc { amount: 5,500, address: "bc1q..." }`. Amount passes the check. ckBTC is burned from the user's ledger subaccount. `RetrieveBtcOk { block_index: N }` is returned.
3. Before the minter's timer fires, Bitcoin fees spike. The minter updates `fee_based_retrieve_btc_min_amount` to 10,000 sat.
4. The minter timer fires, calls `build_batch`, includes the 5,500-sat request, then calls `build_tx`. `build_tx` returns `BuildTxError::AmountTooLow` because 5,500 < current fee.
5. The `AmountTooLow` arm executes: `remove_retrieve_btc_request(s, request, FinalizedStatus::AmountTooLow, runtime)`. No reimbursement is scheduled.
6. User queries `retrieve_btc_status_v2(N)` → `AmountTooLow`. User's ckBTC is gone; no BTC was sent; no reimbursement mint will ever occur. [2](#0-1) [7](#0-6)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L169-171)
```rust
    if args.amount < min_retrieve_amount {
        return Err(RetrieveBtcError::AmountTooLow(min_retrieve_amount));
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L204-232)
```rust
    let burn_memo = BurnMemo::Convert {
        address: Some(&args.address),
        kyt_fee: None,
        status: Some(Status::Accepted),
    };
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

    log!(
        Priority::Debug,
        "accepted a retrieve btc request for {} BTC to address {} (block_index = {})",
        crate::tx::DisplayAmount(request.amount),
        args.address,
        request.block_index
    );

    mutate_state(|s| state::audit::accept_retrieve_btc_request(s, request, &IC_CANISTER_RUNTIME));
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L395-411)
```rust
                    },
                    utxos,
                },
                total_fee,
            )),
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

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L456-457)
```rust
    /// Minimum amount of bitcoin that can be retrieved based on recent fees
    pub fee_based_retrieve_btc_min_amount: u64,
```
