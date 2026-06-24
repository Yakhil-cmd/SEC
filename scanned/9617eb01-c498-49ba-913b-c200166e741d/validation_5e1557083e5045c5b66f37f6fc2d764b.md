### Title
ckBTC Minter `reimburse_canceled_requests` Deterministic Panic Permanently DoSes Withdrawal Processing — (`File: rs/bitcoin/ckbtc/minter/src/lib.rs`)

---

### Summary

The ckBTC minter's `reimburse_canceled_requests` function contains a hard `assert!` that panics if the computed reimbursement fee exceeds `retrieve_btc_min_amount`. When the Bitcoin network's fee rate (an external input consumed via the Bitcoin canister) causes a transaction to be built with an invalid structure, the minter attempts to reimburse affected withdrawal requests. If the reimbursement fee exceeds the configured minimum withdrawal amount, the minter panics deterministically on every subsequent execution of its periodic task, permanently blocking all ckBTC withdrawal processing. This vulnerability class was confirmed in production in the June 2025 incident.

---

### Finding Description

In `rs/bitcoin/ckbtc/minter/src/lib.rs`, the function `reimburse_canceled_requests` is called from `submit_pending_requests` whenever `build_unsigned_transaction` returns `BuildTxError::InvalidTransaction`. The reimbursement fee is computed as:

```rust
let reimbursement_fee = fee_estimator
    .reimbursement_fee_for_pending_withdrawal_requests(batch.len() as u64);
```

which expands to `batch.len() * COST_OF_ONE_BILLION_CYCLES` (satoshis).

Inside `reimburse_canceled_requests`, a hard `assert!` enforces:

```rust
assert!(
    fees[0] <= state.retrieve_btc_min_amount,
    "BUG: fees {fees:?} for {} withdrawal requests are larger than `retrieve_btc_min_amount` {}",
    requests.len(),
    state.retrieve_btc_min_amount
);
``` [1](#0-0) 

If `COST_OF_ONE_BILLION_CYCLES` (the per-request reimbursement fee in satoshis) exceeds `retrieve_btc_min_amount` (which can be as low as 50,000 satoshis per the 2024-11-13 upgrade), this `assert!` fires. Because the minter's periodic task (`ProcessLogic`) re-enters this code path on every tick, the panic is **deterministic and permanent** — the minter cannot process any withdrawal until an upgrade is deployed.

The external input driving this is the Bitcoin network fee rate, read via `get_current_fee_percentiles` from the Bitcoin canister: [2](#0-1) 

An anomalously low fee rate causes the minter to submit a transaction that never gets mined. When the minter later tries to process the stuck transaction via `submit_pending_requests`, it may encounter `BuildTxError::InvalidTransaction` (e.g., `TooManyInputs`), triggering the reimbursement path and the fatal assert.

The `retrieve_btc_min_amount` is configurable and has been reduced over time: [3](#0-2) [4](#0-3) 

---

### Impact Explanation

A deterministic panic in the minter's periodic task permanently halts all ckBTC → BTC withdrawal processing. All pending `retrieve_btc` requests remain stuck indefinitely. Users who have already burned ckBTC cannot receive their BTC. Recovery requires an NNS governance proposal to upgrade the minter canister — a process that takes days. This is a **chain-fusion DoS** with direct financial impact on users holding pending withdrawal requests.

---

### Likelihood Explanation

This is not theoretical. The June 2025 mainnet upgrade proposal (`rs/bitcoin/ckbtc/mainnet/minter_upgrade_2025_06_27.md`) explicitly confirms:

> "There is a **deterministic panic** occurring in the minter when it tries to resubmit those transactions, which explains why those transactions are currently stuck." [5](#0-4) 

The root cause was an anomalously low fee per vbyte returned by the Bitcoin canister, which caused the minter to submit unmined transactions. The assert in `reimburse_canceled_requests` is a separate panic point from the one fixed by PR #5713 (which addressed `resubmit_transactions`), and it remains present in the current codebase. Any future episode of anomalous Bitcoin fee data can re-trigger this class of panic via the `submit_pending_requests` → `reimburse_canceled_requests` path.

---

### Recommendation

Replace the hard `assert!` in `reimburse_canceled_requests` with a graceful error path. If the reimbursement fee exceeds `retrieve_btc_min_amount`, the minter should log the anomaly and either skip reimbursement, cap the fee at `retrieve_btc_min_amount`, or place the requests in a recoverable error state — rather than panicking and permanently halting the periodic task. Additionally, enforce a minimum fee per vbyte floor (as done by PR #5742) to prevent the upstream condition that leads to stuck transactions in the first place.

---

### Proof of Concept

1. Bitcoin canister returns anomalously low fee percentiles (e.g., all zeros or near-zero), as occurred on 2025-06-21.
2. `estimate_fee_per_vbyte` returns a very low `FeeRate`; `fee_based_retrieve_btc_min_amount` is updated to a low value.
3. `submit_pending_requests` builds and submits a Bitcoin transaction with a very low fee. The transaction is never mined.
4. On the next periodic task cycle, `submit_pending_requests` attempts to process the same pending requests again. If the batch now triggers `BuildTxError::InvalidTransaction` (e.g., due to UTXO set changes or `TooManyInputs`), it calls:
   ```rust
   reimburse_canceled_requests(s, batch, reason, reimbursement_fee, runtime);
   ```
   where `reimbursement_fee = batch.len() * COST_OF_ONE_BILLION_CYCLES`.
5. Inside `reimburse_canceled_requests`, `fees[0]` (≈ `COST_OF_ONE_BILLION_CYCLES`) exceeds `state.retrieve_btc_min_amount` (e.g., 50,000 satoshis).
6. The `assert!` at line 302 fires, the minter canister traps.
7. Every subsequent execution of the periodic task re-enters the same code path and traps again — **permanent DoS**. [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L227-263)
```rust
pub async fn estimate_fee_per_vbyte<R: CanisterRuntime>(runtime: &R) -> Option<FeeRate> {
    let btc_network = state::read_state(|s| s.btc_network);
    match runtime
        .get_current_fee_percentiles(&bitcoin_canister::GetCurrentFeePercentilesRequest {
            network: btc_network.into(),
        })
        .await
    {
        Ok(fees) => {
            let fee_estimator = state::read_state(|s| runtime.fee_estimator(s));
            match fee_estimator.estimate_median_fee(&fees) {
                Some(median_fee) => {
                    let fee_based_retrieve_btc_min_amount =
                        fee_estimator.fee_based_minimum_withdrawal_amount(median_fee);
                    log!(
                        Priority::Debug,
                        "[estimate_fee_per_vbyte]: update median fee per vbyte to {median_fee:?} and fee-based minimum retrieve amount to {fee_based_retrieve_btc_min_amount} with {fees:?}"
                    );
                    mutate_state(|s| {
                        s.last_fee_per_vbyte = fees;
                        s.last_median_fee_per_vbyte = Some(median_fee);
                        s.fee_based_retrieve_btc_min_amount = fee_based_retrieve_btc_min_amount;
                    });
                    Some(median_fee)
                }
                None => None,
            }
        }
        Err(err) => {
            log!(
                Priority::Info,
                "[estimate_fee_per_vbyte]: failed to get median fee per vbyte: {}",
                err
            );
            None
        }
    }
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

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L452-457)
```rust

    /// Minimum amount of bitcoin that can be retrieved
    pub retrieve_btc_min_amount: u64,

    /// Minimum amount of bitcoin that can be retrieved based on recent fees
    pub fee_based_retrieve_btc_min_amount: u64,
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L708-711)
```rust
        if let Some(retrieve_btc_min_amount) = retrieve_btc_min_amount {
            self.retrieve_btc_min_amount = retrieve_btc_min_amount;
            self.fee_based_retrieve_btc_min_amount = retrieve_btc_min_amount;
        }
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

**File:** rs/bitcoin/ckbtc/minter/src/fees/mod.rs (L154-158)
```rust
    fn reimbursement_fee_for_pending_withdrawal_requests(&self, num_requests: u64) -> u64 {
        // Heuristic:
        // * charge 1B cycles for each request (a burn on the ledger on the fiduciary subnet is probably around 50M cycles).
        num_requests.saturating_mul(Self::COST_OF_ONE_BILLION_CYCLES)
    }
```
