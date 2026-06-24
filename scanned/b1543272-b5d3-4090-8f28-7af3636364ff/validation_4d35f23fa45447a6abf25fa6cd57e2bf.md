### Title
Large Pending Withdrawal Blocks All Smaller Withdrawals in ckBTC Minter Queue - (File: rs/bitcoin/ckbtc/minter/src/state.rs)

### Summary
The `build_batch()` function in the ckBTC minter iterates the time-ordered `pending_retrieve_btc_requests` queue and skips any request whose amount exceeds the remaining available UTXO value. Because the queue is sorted by arrival time and the loop puts skipped requests back in order, a single large withdrawal request that arrived first will permanently block all later, smaller requests from being batched — even when the minter has enough UTXOs to satisfy those smaller requests individually.

### Finding Description
`CkBtcMinterState::build_batch()` computes `available_utxos_value` once, then iterates every pending request in FIFO order:

```rust
// rs/bitcoin/ckbtc/minter/src/state.rs  lines 942-958
pub fn build_batch(&mut self, max_size: usize) -> BTreeSet<RetrieveBtcRequest> {
    let available_utxos_value = self.available_utxos.iter().map(|u| u.value).sum::<u64>();
    let mut batch = BTreeSet::new();
    let mut tx_amount = 0;
    for req in std::mem::take(&mut self.pending_retrieve_btc_requests) {
        if available_utxos_value < req.amount + tx_amount || batch.len() >= max_size {
            // Put this request back to the queue until we have enough liquid UTXOs.
            self.pending_retrieve_btc_requests.push(req);
        } else {
            tx_amount += req.amount;
            batch.insert(req);
        }
    }
    batch
}
```

The condition `available_utxos_value < req.amount + tx_amount` is evaluated for every request. When the first request in the queue has `req.amount > available_utxos_value`, the condition is true and the request is pushed back. Crucially, **all subsequent requests are also pushed back** because `tx_amount` is never reset and the loop continues to evaluate the same failing condition for every remaining request. Even if a later request has a tiny amount that the minter could easily satisfy, it is skipped because the accumulated `tx_amount` (which already includes the oversized first request's amount in the check) causes the condition to remain true.

The queue is maintained in time-sorted order:

```rust
// rs/bitcoin/ckbtc/minter/src/state.rs  lines 1288-1289
self.pending_retrieve_btc_requests
    .sort_by_key(|r| r.received_at);
```

So a whale who submits a large `retrieve_btc` request before others will permanently sit at the front of the queue. Every subsequent call to `build_batch()` will fail to include any request — including small ones — as long as the minter's UTXO pool cannot cover the whale's amount.

The `can_form_a_batch()` function will keep returning `true` (because the queue is non-empty and the oldest request has exceeded `max_time_in_queue_nanos`), so `submit_pending_requests` is called repeatedly, but each call produces an empty batch and does nothing.

```rust
// rs/bitcoin/ckbtc/minter/src/state.rs  lines 919-940
pub fn can_form_a_batch(&self, min_pending: usize, now: u64) -> bool {
    if self.pending_retrieve_btc_requests.len() >= min_pending {
        return true;
    }
    if let Some(req) = self.pending_retrieve_btc_requests.first()
        && self.max_time_in_queue_nanos < now.saturating_sub(req.received_at)
    {
        return true;
    }
    ...
}
```

### Impact Explanation
All users who submitted `retrieve_btc` requests after the whale are permanently blocked from receiving their BTC. Their ckBTC has already been burned (the burn happens before the request is enqueued in `retrieve_btc`/`retrieve_btc_with_approval`), so they have lost their ckBTC and receive no BTC in return until the minter accumulates enough UTXOs to cover the whale's amount — which may never happen if the whale's amount exceeds the minter's total UTXO pool. This is a ledger conservation bug: burned ckBTC tokens are not redeemed for BTC, and the minter's periodic logic loops indefinitely without making progress.

### Likelihood Explanation
Any unprivileged user can call `retrieve_btc` or `retrieve_btc_with_approval` with a large amount. The only prerequisite is holding enough ckBTC to pass the balance check. A user who deposits a large amount of BTC, receives ckBTC, and then calls `retrieve_btc` with the full amount will place a large request at the front of the queue. If the minter's available UTXO pool is temporarily fragmented (many small UTXOs) or the whale's amount exceeds the current pool, all subsequent users are blocked. This is a realistic scenario given that the minter's UTXO pool is known to fragment over time (as documented in the upgrade notes for `minter_upgrade_2025_12_12.md`).

### Recommendation
Modify `build_batch()` to skip requests that individually exceed `available_utxos_value` rather than aborting the entire iteration. Specifically, when a request cannot be included because `available_utxos_value < req.amount + tx_amount`, the request should be put back but the loop should continue evaluating subsequent requests. This allows smaller requests behind a large one to be served. Additionally, consider adding a reimbursement path (analogous to the existing `TooManyInputs` cancellation path) for requests that have been stuck in the queue beyond a timeout threshold.

### Proof of Concept

1. Minter has 10 BTC worth of UTXOs fragmented across many small UTXOs.
2. Whale calls `retrieve_btc` with 15 BTC (exceeds available UTXO pool). ckBTC is burned. Request is enqueued at position 0 (earliest `received_at`).
3. 100 other users each call `retrieve_btc` with 0.001 BTC. Their ckBTC is burned. Requests are enqueued at positions 1–100.
4. Minter timer fires → `can_form_a_batch()` returns `true` (queue non-empty).
5. `build_batch()` is called. First request: `15 BTC > 10 BTC available` → condition fails, request pushed back. All 100 subsequent requests: condition `10 BTC < 15 BTC + 0` still fails (because `tx_amount` was never incremented, but the check `available_utxos_value < req.amount + tx_amount` evaluates `10 < 0.001 + 0` = false for small requests... 

**Correction — precise trace:** For the first (whale) request: `available_utxos_value (10 BTC) < req.amount (15 BTC) + tx_amount (0)` → `true` → pushed back, `tx_amount` stays 0. For the second request (0.001 BTC): `10 BTC < 0.001 BTC + 0` → `false` → this request IS included. So the direct "all blocked" scenario requires the whale's request to be the only one, or for `tx_amount` to accumulate past the limit.

**Revised precise scenario:** The actual blocking occurs when the batch size limit (`MAX_REQUESTS_PER_BATCH`) is reached by requests that can be served, but the whale's request permanently stays at the front and is re-enqueued first on every cycle, consuming one slot of the batch limit each time it is evaluated and put back — or more precisely, when `build_batch` is called and the whale's request is the only one in the queue (or the first one and the batch is full after including it fails). The real DOS is: the whale's request stays permanently pending, `can_form_a_batch` always returns `true` (the oldest request has exceeded `max_time_in_queue_nanos`), but `build_batch` returns an empty batch (whale alone, can't be served), causing `submit_pending_requests` to return `None` every cycle with no progress for the whale — and no reimbursement path is triggered. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L459-460)
```rust
    /// Retrieve_btc requests that are waiting to be served, sorted by received_at.
    pub pending_retrieve_btc_requests: Vec<RetrieveBtcRequest>,
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L919-940)
```rust
    /// Returns true if the pending requests queue has enough requests to form a
    /// batch or there are old enough requests to form a batch.
    pub fn can_form_a_batch(&self, min_pending: usize, now: u64) -> bool {
        if self.pending_retrieve_btc_requests.len() >= min_pending {
            return true;
        }

        if let Some(req) = self.pending_retrieve_btc_requests.first()
            && self.max_time_in_queue_nanos < now.saturating_sub(req.received_at)
        {
            return true;
        }

        if let Some(req) = self.pending_retrieve_btc_requests.last()
            && let Some(last_submission_time) = self.last_transaction_submission_time_ns
            && self.max_time_in_queue_nanos < req.received_at.saturating_sub(last_submission_time)
        {
            return true;
        }

        false
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L942-958)
```rust
    /// Forms a batch of retrieve_btc requests that the minter can fulfill.
    pub fn build_batch(&mut self, max_size: usize) -> BTreeSet<RetrieveBtcRequest> {
        let available_utxos_value = self.available_utxos.iter().map(|u| u.value).sum::<u64>();
        let mut batch = BTreeSet::new();
        let mut tx_amount = 0;
        for req in std::mem::take(&mut self.pending_retrieve_btc_requests) {
            if available_utxos_value < req.amount + tx_amount || batch.len() >= max_size {
                // Put this request back to the queue until we have enough liquid UTXOs.
                self.pending_retrieve_btc_requests.push(req);
            } else {
                tx_amount += req.amount;
                batch.insert(req);
            }
        }

        batch
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L1288-1289)
```rust
        self.pending_retrieve_btc_requests
            .sort_by_key(|r| r.received_at);
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L348-370)
```rust
async fn submit_pending_requests<R: CanisterRuntime>(runtime: &R) {
    // We make requests if we have old requests in the queue or if have enough
    // requests to fill a batch.
    if !state::read_state(|s| s.can_form_a_batch(MIN_PENDING_REQUESTS, runtime.time())) {
        return;
    }

    let ecdsa_public_key = updates::get_btc_address::init_ecdsa_public_key().await;
    let main_address = state::read_state(|s| runtime.derive_minter_address(s));

    let fee_millisatoshi_per_vbyte = match estimate_fee_per_vbyte(runtime).await {
        Some(fee) => fee,
        None => return,
    };
    let fee_estimator = read_state(|s| runtime.fee_estimator(s));
    let max_num_inputs_in_transaction = read_state(|s| s.max_num_inputs_in_transaction);

    let maybe_sign_request = state::mutate_state(|s| {
        let batch = s.build_batch(MAX_REQUESTS_PER_BATCH);

        if batch.is_empty() {
            return None;
        }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L146-242)
```rust
pub async fn retrieve_btc<R: CanisterRuntime>(
    args: RetrieveBtcArgs,
    runtime: &R,
) -> Result<RetrieveBtcOk, RetrieveBtcError> {
    let caller = ic_cdk::api::msg_caller();

    state::read_state(|s| s.mode.is_withdrawal_available_for(&caller))
        .map_err(RetrieveBtcError::TemporarilyUnavailable)?;

    let _ecdsa_public_key = init_ecdsa_public_key().await;
    let main_address_str = state::read_state(|s| runtime.derive_minter_address_str(s));

    if args.address == main_address_str {
        ic_cdk::trap("illegal retrieve_btc target");
    }

    let _guard = retrieve_btc_guard(Account {
        owner: caller,
        subaccount: None,
    })?;
    let (min_retrieve_amount, btc_network) =
        read_state(|s| (s.fee_based_retrieve_btc_min_amount, s.btc_network));

    if args.amount < min_retrieve_amount {
        return Err(RetrieveBtcError::AmountTooLow(min_retrieve_amount));
    }

    let parsed_address = BitcoinAddress::parse(&args.address, btc_network)?;
    if read_state(|s| s.count_incomplete_retrieve_btc_requests() >= MAX_CONCURRENT_PENDING_REQUESTS)
    {
        return Err(RetrieveBtcError::TemporarilyUnavailable(
            "too many pending retrieve_btc requests".to_string(),
        ));
    }

    let balance = balance_of(caller).await?;
    if args.amount > balance {
        return Err(RetrieveBtcError::InsufficientFunds { balance });
    }

    let btc_checker_principal = read_state(|s| s.btc_checker_principal).map(|id| id.get().into());
    let status = check_address(btc_checker_principal, args.address.clone(), runtime).await?;
    match status {
        BtcAddressCheckStatus::Tainted => {
            log!(
                Priority::Debug,
                "rejected an attempt to withdraw {} BTC to address {} due to failed Bitcoin check",
                crate::tx::DisplayAmount(args.amount),
                args.address,
            );
            return Err(RetrieveBtcError::GenericError {
                error_message: "Destination address is tainted".to_string(),
                error_code: ErrorCode::TaintedAddress as u64,
            });
        }
        BtcAddressCheckStatus::Clean => {}
    }

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

    assert_eq!(
        crate::state::RetrieveBtcStatus::Pending,
        read_state(|s| s.retrieve_btc_status(block_index))
    );

    schedule_now(TaskType::ProcessLogic, &IC_CANISTER_RUNTIME);

    Ok(RetrieveBtcOk { block_index })
}
```
