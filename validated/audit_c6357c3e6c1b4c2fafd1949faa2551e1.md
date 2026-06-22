### Title
Single-Input Vsize Assumption in `fee_based_minimum_withdrawal_amount` Allows `AmountTooLow` Finalization Without Reimbursement — (`rs/bitcoin/ckbtc/minter/src/fees/mod.rs`)

---

### Summary

`fee_based_minimum_withdrawal_amount` computes the minimum withdrawal threshold using a hard-coded single-input transaction vsize bound (`PER_REQUEST_VSIZE_BOUND = 221` vbytes). When the minter's UTXO pool contains only UTXOs smaller than the withdrawal amount, `greedy` selects N > 1 inputs, and the actual fee computed by `evaluate_transaction_fee` against the real multi-input vsize can exceed the withdrawal amount. This triggers `BuildTxError::AmountTooLow` in `build_unsigned_transaction_from_inputs`, which is handled in `submit_pending_requests` by finalizing the request with `FinalizedStatus::AmountTooLow` — **with no reimbursement** — burning the user's ckBTC without delivering BTC.

---

### Finding Description

**Root cause — fee formula uses 1-input vsize:** [1](#0-0) 

`PER_REQUEST_VSIZE_BOUND = 221` vbytes is the vsize of a 1-input/2-output P2WPKH transaction. The formula rounds the estimated fee overhead down to the nearest 50,000 sats and adds `retrieve_btc_min_amount`. It never accounts for the possibility that N > 1 inputs are needed.

**UTXO selection — `greedy` picks the smallest UTXO ≥ goal, falling back to the largest:** [2](#0-1) 

When all UTXOs in the pool are smaller than the withdrawal amount, `find_lower_bound(goal)` returns `None` on every iteration and `last()` (the largest available) is taken repeatedly. For a withdrawal of W sats with all UTXOs worth V sats (V < W), exactly `ceil(W/V)` UTXOs are selected.

**Actual fee check uses real vsize:** [3](#0-2) 

`evaluate_transaction_fee` calls `fake_sign(tx).vsize()` on the fully-constructed N-input transaction. For N = 20 inputs at 116 sats/vbyte, vsize ≈ 1,432 vbytes → fee ≈ 166,112 sats, which exceeds a 100,000-sat withdrawal amount.

**`AmountTooLow` path burns funds without reimbursement:** [4](#0-3) [5](#0-4) 

`remove_retrieve_btc_request` records `EventType::RemovedRetrieveBtcRequest` and pushes `FinalizedBtcRequest { state: FinalizedStatus::AmountTooLow }`. No `schedule_withdrawal_reimbursement` event is emitted. The ckBTC was already burned in `retrieve_btc` before the request entered the pending queue.

Contrast with `BuildTxError::InvalidTransaction`, which **does** trigger `reimburse_canceled_requests`: [6](#0-5) 

---

### Impact Explanation

A user who submits a withdrawal at exactly `fee_based_retrieve_btc_min_amount` when the minter's UTXO pool contains only UTXOs smaller than that amount will have their ckBTC permanently burned with no BTC delivery and no reimbursement. The `RetrieveBtcStatusV2::AmountTooLow` terminal state is returned with no recovery path.

---

### Likelihood Explanation

**Precondition**: The minter's pool must contain no single UTXO ≥ the withdrawal amount. This can occur:

1. **Naturally**: After a period of many small deposits and large withdrawals consuming all large UTXOs.
2. **Adversarially**: An attacker deposits many small UTXOs (each just above `deposit_btc_min_amount = check_fee + 1`) to fill the pool. The attacker spends real BTC but does not directly profit — this is a griefing attack against any user who subsequently withdraws at the minimum amount.

The `greedy` algorithm's preference for a single large UTXO (it picks the smallest UTXO ≥ goal first) means the attack only activates when **no** UTXO in the pool is ≥ the withdrawal amount — a meaningful but achievable constraint.

The UTXO consolidation mechanism provides partial mitigation but requires `utxo_consolidation_threshold` UTXOs to be present and has a `MIN_CONSOLIDATION_INTERVAL` of 24 hours.

---

### Recommendation

1. **Fix the vsize bound**: Replace `PER_REQUEST_VSIZE_BOUND = 221` with a bound that accounts for multi-input transactions, or compute `fee_based_minimum_withdrawal_amount` using the actual UTXO pool composition (as `estimate_withdrawal_fee` already does for the query endpoint).

2. **Add reimbursement for `AmountTooLow`**: When `BuildTxError::AmountTooLow` is triggered in `submit_pending_requests`, reimburse the user (minus a processing fee) rather than silently burning their ckBTC. The `InvalidTransaction` path already does this correctly and should serve as the model.

3. **Address the TODO**: The comment at line 141 (`//TODO DEFI-2187: adjust increment of minimum withdrawal amount to be a multiple of retrieve_btc_min_amount/2`) acknowledges the formula is known to be imprecise.

---

### Proof of Concept

State-machine test setup (Regtest or Testnet):

```
retrieve_btc_min_amount = 50_000 sats
fee_rate = 116_000 millisats/vbyte  (→ fee_based_retrieve_btc_min_amount = 100_000 sats)
UTXO pool: 20 UTXOs × 5_000 sats each (total = 100_000 sats, no single UTXO ≥ 100_000)

greedy(100_000) selects all 20 UTXOs.
Actual tx vsize (20 inputs, 2 outputs P2WPKH) ≈ 10 + 68×20 + 62 = 1,432 vbytes
Actual bitcoin_fee = ceil(1_432 × 116_000 / 1_000) = 166,112 sats
Minter fee = max(146×20 + 4×2 + 26, 300) = 2,946 sats
Total fee = 169,058 sats > 100,000 sats → BuildTxError::AmountTooLow

Expected: withdrawal succeeds or user is reimbursed.
Actual: request finalized as AmountTooLow, ckBTC burned, no BTC delivered, no reimbursement.
```

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/fees/mod.rs (L133-143)
```rust
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

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L1070-1100)
```rust
fn greedy(target: u64, available_utxos: &mut UtxoSet) -> Vec<Utxo> {
    #[cfg(feature = "canbench-rs")]
    let _scope = canbench_rs::bench_scope("greedy");

    let mut solution = vec![];
    let mut goal = target;
    while goal > 0 {
        let candidate_utxo = available_utxos
            .find_lower_bound(goal)
            .or_else(|| available_utxos.last())
            .cloned();
        match candidate_utxo {
            Some(utxo) => {
                let utxo = available_utxos.remove(&utxo).expect("BUG: missing UTXO");
                goal = goal.saturating_sub(utxo.value);
                solution.push(utxo);
            }
            None => {
                // Not enough available UTXOs to satisfy the request.
                for u in solution {
                    available_utxos.insert(u);
                }
                return vec![];
            }
        }
    }

    debug_assert!(solution.is_empty() || solution.iter().map(|u| u.value).sum::<u64>() >= target);

    solution
}
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L1304-1308)
```rust
    let fee = fee_estimator.evaluate_transaction_fee(&unsigned_tx, fee_rate);

    if fee + minter_fee > amount {
        return Err(BuildTxError::AmountTooLow);
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/state/audit.rs (L67-83)
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
```
