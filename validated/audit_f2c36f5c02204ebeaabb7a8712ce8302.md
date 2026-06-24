Audit Report

## Title
Single-Input Vsize Assumption in `fee_based_minimum_withdrawal_amount` Causes Unreimbursed ckBTC Burn on `AmountTooLow` — (`rs/bitcoin/ckbtc/minter/src/fees/mod.rs`)

## Summary

`fee_based_minimum_withdrawal_amount` computes the minimum withdrawal threshold using a hard-coded single-input vsize constant (`PER_REQUEST_VSIZE_BOUND = 221`). When the minter's UTXO pool contains only UTXOs smaller than the withdrawal amount, `greedy` selects N > 1 inputs, and the actual fee computed against the real multi-input vsize can exceed the withdrawal amount. This triggers `BuildTxError::AmountTooLow`, which finalizes the request as `FinalizedStatus::AmountTooLow` with no reimbursement — permanently burning the user's ckBTC without delivering BTC.

## Finding Description

**Root cause — single-input vsize in minimum amount formula:**

`fee_based_minimum_withdrawal_amount` uses `PER_REQUEST_VSIZE_BOUND = 221` vbytes, which is the vsize of a 1-input/2-output P2WPKH transaction. [1](#0-0) 

This formula never accounts for multi-input transactions. The result is that a user can submit a withdrawal at exactly `fee_based_retrieve_btc_min_amount` and still trigger `AmountTooLow` at execution time.

**UTXO selection — `greedy` picks multiple small UTXOs:**

When `find_lower_bound(goal)` returns `None` (no single UTXO ≥ goal), `greedy` falls back to `last()` (the largest available) on every iteration, accumulating many small UTXOs. [2](#0-1) 

**Actual fee check uses real vsize of the N-input transaction:**

`evaluate_transaction_fee` calls `fake_sign(tx).vsize()` on the fully-constructed transaction, so the fee scales with the actual number of inputs. [3](#0-2) 

The check at `build_unsigned_transaction_from_inputs` then returns `BuildTxError::AmountTooLow` when the real fee exceeds the withdrawal amount. [4](#0-3) 

**`AmountTooLow` path finalizes without reimbursement:**

The `AmountTooLow` branch in `submit_pending_requests` calls `remove_retrieve_btc_request` with `FinalizedStatus::AmountTooLow` for each request in the batch, emitting only `EventType::RemovedRetrieveBtcRequest` — no `schedule_withdrawal_reimbursement` event is emitted. [5](#0-4) [6](#0-5) 

**Contrast with `InvalidTransaction`, which does reimburse:**

The `InvalidTransaction` branch calls `reimburse_canceled_requests`, correctly returning funds minus a processing fee. [7](#0-6) 

The ckBTC burn occurs during `retrieve_btc` before the request enters the pending queue, so by the time `AmountTooLow` is reached, the user's ckBTC is already gone with no recovery path.

## Impact Explanation

This is a **High** severity finding. A normal user submitting a withdrawal at the published minimum amount (`fee_based_retrieve_btc_min_amount`) can have their ckBTC permanently burned with no BTC delivered and no reimbursement. This constitutes a concrete, permanent loss of ck-token assets — matching the allowed impact: *"Significant Chain Fusion, ck-token, ledger... security impact with concrete user or protocol harm."*

## Likelihood Explanation

The precondition — no single UTXO in the minter pool ≥ the withdrawal amount — can arise naturally after a period of many small deposits and large withdrawals consuming all large UTXOs. It can also be induced adversarially: an attacker deposits many small UTXOs (each just above `deposit_btc_min_amount`) to fill the pool, then any user who subsequently withdraws at the minimum amount is affected. The adversary spends real BTC but does not profit directly (griefing). The `greedy` algorithm's preference for a single large UTXO means the condition only activates when **no** UTXO ≥ the withdrawal amount, which is a meaningful but achievable constraint. UTXO consolidation provides partial mitigation but has a 24-hour minimum interval and a threshold requirement.

## Recommendation

1. **Fix the vsize bound**: Replace `PER_REQUEST_VSIZE_BOUND = 221` with a conservative multi-input bound, or compute `fee_based_minimum_withdrawal_amount` using the actual UTXO pool composition (as `estimate_withdrawal_fee` already does for the query endpoint). The existing TODO at line 141 (`//TODO DEFI-2187`) acknowledges the formula is known to be imprecise. [8](#0-7) 

2. **Add reimbursement for `AmountTooLow`**: When `BuildTxError::AmountTooLow` is triggered in `submit_pending_requests`, apply the same `reimburse_canceled_requests` logic used for `InvalidTransaction`, returning funds minus a processing fee rather than silently burning them. [5](#0-4) 

## Proof of Concept

State-machine test (Regtest or PocketIC):

```
retrieve_btc_min_amount = 50_000 sats
fee_rate = 116_000 millisats/vbyte
→ fee_based_retrieve_btc_min_amount = 100_000 sats

UTXO pool: 20 UTXOs × 5_000 sats each (total = 100_000 sats, no single UTXO ≥ 100_000)

1. User calls retrieve_btc(amount=100_000) → ckBTC burned, request queued.
2. submit_pending_requests runs:
   - greedy(100_000) selects all 20 UTXOs (each 5_000 sats).
   - Actual tx vsize (20 inputs, 2 outputs P2WPKH) ≈ 1,432 vbytes.
   - bitcoin_fee = ceil(1_432 × 116_000 / 1_000) = 166,112 sats.
   - 166,112 > 100,000 → BuildTxError::AmountTooLow.
3. Request finalized as FinalizedStatus::AmountTooLow.
   No BTC sent. No reimbursement. ckBTC permanently burned.

Expected: withdrawal succeeds or user is reimbursed.
Actual: ckBTC burned, no BTC delivered, no reimbursement.
```

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/fees/mod.rs (L133-144)
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
            }
```

**File:** rs/bitcoin/ckbtc/minter/src/fees/mod.rs (L149-152)
```rust
    fn evaluate_transaction_fee(&self, tx: &UnsignedTransaction, fee_rate: FeeRate) -> u64 {
        let tx_vsize = fake_sign(tx).vsize();
        fee_rate.fee_ceil(tx_vsize as u64)
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

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L1076-1095)
```rust
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
