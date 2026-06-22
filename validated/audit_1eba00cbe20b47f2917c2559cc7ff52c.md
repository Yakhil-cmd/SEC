## Analysis

Let me trace the full code path to determine if this is a real vulnerability.

### Step 1: `retrieve_btc` acceptance check

The handler reads `fee_based_retrieve_btc_min_amount` at call time: [1](#0-0) 

The ckBTC is then burned (an async call) before the request is queued: [2](#0-1) 

### Step 2: `submit_pending_requests` fetches a fresh fee rate

When the timer fires, `estimate_fee_per_vbyte` is called, which contacts the Bitcoin canister (async) and **atomically updates** `fee_based_retrieve_btc_min_amount` in state: [3](#0-2) 

The freshly fetched fee rate is then passed directly to `build_unsigned_transaction`: [4](#0-3) 

### Step 3: `AmountTooLow` check inside `build_unsigned_transaction_from_inputs` [5](#0-4) 

### Step 4: What happens on `AmountTooLow`?

The request is finalized with **no reimbursement**: [6](#0-5) 

`remove_retrieve_btc_request` only records an event and pushes to finalized — it does **not** schedule a reimbursement: [7](#0-6) 

Contrast this with `BuildTxError::InvalidTransaction`, which **does** call `reimburse_canceled_requests`: [8](#0-7) 

### Step 5: Is the buffer in `fee_based_minimum_withdrawal_amount` sufficient?

The formula provides a fixed RBF buffer of 22,100 sats: [9](#0-8) 

For a 221-vbyte transaction, `PER_REQUEST_RBF_BOUND = 22_100` equates to ~100 sat/vbyte of headroom. If the fee rate increases by more than ~100 sat/vbyte between acceptance and batch processing, the minimum amount accepted at `retrieve_btc` time will be insufficient at `build_unsigned_transaction` time.

**Concrete example:**
- Fee at `retrieve_btc` time: 1 sat/vbyte → `fee_based_retrieve_btc_min_amount` = 100,000 sats (rounds to `retrieve_btc_min_amount`)
- Fee at batch processing time: 500 sat/vbyte → actual fee = 500 × 221 = 110,500 sats
- `110,500 + 300 (minter_fee) > 100,000` → `BuildTxError::AmountTooLow` → ckBTC burned, no BTC, no reimbursement

The code itself acknowledges that fee estimation can be wildly off — a comment added around 2025-06-21 notes a real incident where the estimate was 142 millisat/vbyte when the actual median was 2,000 millisat/vbyte (~14× discrepancy): [10](#0-9) 

---

### Title
Fee spike between `retrieve_btc` acceptance and batch processing causes ckBTC burn with no BTC delivery and no reimbursement — (`rs/bitcoin/ckbtc/minter/src/lib.rs`, `rs/bitcoin/ckbtc/minter/src/fees/mod.rs`)

### Summary
The `fee_based_retrieve_btc_min_amount` guard in `retrieve_btc` is evaluated at call time using the last cached fee rate. When `submit_pending_requests` runs, it fetches a fresh fee rate from the Bitcoin canister and uses it for `build_unsigned_transaction`. If the fee rate has spiked by more than ~100 sat/vbyte since the minimum was computed, `BuildTxError::AmountTooLow` is returned. Unlike `InvalidTransaction`, the `AmountTooLow` branch finalizes the request without scheduling any reimbursement, permanently destroying the user's ckBTC.

### Finding Description
1. User calls `retrieve_btc` with `amount == fee_based_retrieve_btc_min_amount` (e.g., 100,000 sats at 1 sat/vbyte fee environment). The check passes, ckBTC is burned.
2. Before the timer fires, Bitcoin network fees spike (e.g., to 500 sat/vbyte).
3. `submit_pending_requests` calls `estimate_fee_per_vbyte`, which fetches the new high fee and updates `fee_based_retrieve_btc_min_amount` to a higher value.
4. `build_unsigned_transaction` is called with the new fee rate. For the accepted request, `fee + minter_fee > amount` → `BuildTxError::AmountTooLow`.
5. The `AmountTooLow` arm calls `remove_retrieve_btc_request(..., FinalizedStatus::AmountTooLow, ...)` — no `reimburse_canceled_requests`, no `schedule_withdrawal_reimbursement`. The ckBTC is gone.

The fixed RBF buffer (`PER_REQUEST_RBF_BOUND = 22_100`) only covers ~100 sat/vbyte of fee increase headroom for a 221-vbyte transaction. Bitcoin fees routinely spike by multiples of this during congestion events.

### Impact Explanation
User loses ckBTC permanently. The ckBTC ledger records a burn; the minter records `FinalizedStatus::AmountTooLow`; no BTC is sent; no ckBTC is minted back. The `WithdrawalReimbursementReason` enum has no `AmountTooLow` variant, so the reimbursement path is structurally absent.

### Likelihood Explanation
Bitcoin fee spikes exceeding 100 sat/vbyte from a low base are historically common (Ordinals, BRC-20 events, halving periods). The minter's own codebase documents a real incident (June 2025) where the fee estimate was off by 14×. The window between `retrieve_btc` acceptance and batch processing can be minutes to hours (`max_time_in_queue_nanos`). This is not a theoretical edge case.

### Recommendation
1. **Reimburse on `AmountTooLow`**: Treat `BuildTxError::AmountTooLow` the same as `InvalidTransaction` — call `reimburse_canceled_requests` so the user's ckBTC is returned minus a small processing fee.
2. **Re-validate minimum at batch time**: Before finalizing a request as `AmountTooLow`, check whether the request amount was above the minimum at acceptance time; if so, reimburse in full.
3. **Increase the RBF buffer**: Make `PER_REQUEST_RBF_BOUND` a function of the current fee rate (e.g., a multiplier) rather than a fixed constant, so the minimum scales with fee volatility.

### Proof of Concept
State-machine test:
1. Initialize minter with fee percentiles at 1 sat/vbyte → `fee_based_retrieve_btc_min_amount` = 100,000 sats.
2. Call `retrieve_btc(amount=100_000)` → accepted, ckBTC burned.
3. Set fee percentiles to 500 sat/vbyte.
4. Tick the timer → `submit_pending_requests` → `estimate_fee_per_vbyte` updates state → `build_unsigned_transaction` with 500 sat/vbyte → `AmountTooLow`.
5. Assert: `retrieve_btc_status(block_index) == AmountTooLow` and no reimbursement minted on the ledger.

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L166-171)
```rust
    let (min_retrieve_amount, btc_network) =
        read_state(|s| (s.fee_based_retrieve_btc_min_amount, s.btc_network));

    if args.amount < min_retrieve_amount {
        return Err(RetrieveBtcError::AmountTooLow(min_retrieve_amount));
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L209-210)
```rust
    let block_index =
        burn_ckbtcs(caller, args.amount, crate::memo::encode(&burn_memo).into()).await?;
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L245-249)
```rust
                    mutate_state(|s| {
                        s.last_fee_per_vbyte = fees;
                        s.last_median_fee_per_vbyte = Some(median_fee);
                        s.fee_based_retrieve_btc_min_amount = fee_based_retrieve_btc_min_amount;
                    });
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L358-384)
```rust
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

        let outputs: Vec<_> = batch
            .iter()
            .map(|req| (req.address.clone(), req.amount))
            .collect();

        match build_unsigned_transaction(
            &mut s.available_utxos,
            outputs,
            &main_address,
            max_num_inputs_in_transaction,
            fee_millisatoshi_per_vbyte,
            &fee_estimator,
        ) {
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

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L1304-1308)
```rust
    let fee = fee_estimator.evaluate_transaction_fee(&unsigned_tx, fee_rate);

    if fee + minter_fee > amount {
        return Err(BuildTxError::AmountTooLow);
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

**File:** rs/bitcoin/ckbtc/minter/src/fees/mod.rs (L77-87)
```rust
    /// An estimated fee per vbyte of 142 millisatoshis per vbyte was selected around 2025.06.21 01:09:50 UTC
    /// for Bitcoin Mainnet, whereas the median fee around that time should have been 2_000.
    /// Until we know the root cause, we ensure that the estimated fee has a meaningful minimum value.
    const fn minimum_fee_per_vbyte(&self) -> FeeRate {
        let rate = match &self.network {
            Network::Mainnet => 1_500,
            Network::Testnet => 1_000,
            Network::Regtest => 0,
        };
        FeeRate::from_millis_per_byte(rate)
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/fees/mod.rs (L130-147)
```rust
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
