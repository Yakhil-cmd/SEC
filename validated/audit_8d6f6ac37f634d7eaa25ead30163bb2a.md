Audit Report

## Title
UTXO Consolidation Depletes `available_utxos` Without Reserving Value for Pending Withdrawal Requests - (File: `rs/bitcoin/ckbtc/minter/src/lib.rs`)

## Summary
`consolidate_utxos()` in the ckBTC minter removes up to `max_num_inputs_in_transaction` UTXOs from `available_utxos` via `select_utxos_to_consolidate()` without first checking whether those UTXOs are needed to fulfill entries in `pending_retrieve_btc_requests`. After consolidation submits its Bitcoin transaction, the removed UTXOs are locked in `submitted_transactions` until the transaction receives `min_confirmations` on-chain. Any pending withdrawal request whose required value exceeds the remaining `available_utxos` value is silently re-queued by `build_batch()` and cannot be processed until the consolidation transaction confirms, leaving the user holding neither ckBTC (already burned) nor BTC for the duration of Bitcoin confirmation.

## Finding Description

**Root cause — no pending-request check in `consolidate_utxos()`:**

`consolidate_utxos()` passes three guards before removing UTXOs: [1](#0-0) 

None of these guards inspect `pending_retrieve_btc_requests`. Immediately after, `select_utxos_to_consolidate()` destructively pops UTXOs from `available_utxos`: [2](#0-1) [3](#0-2) 

**UTXOs are locked until Bitcoin confirmation:**

After `sign_and_submit_request` completes, the used UTXOs live in `submitted_transactions` and are not returned to `available_utxos` until `finalize_transaction` is called for the `ToConsolidate` variant, which only happens after `min_confirmations` on-chain confirmations: [4](#0-3) 

**`build_batch()` reads the now-depleted pool:**

When `ProcessLogic` next runs `submit_pending_requests`, `build_batch()` computes `available_utxos_value` from the current (depleted) set and re-queues any request it cannot cover: [5](#0-4) 

**`TimerLogicGuard` does not prevent the sequential depletion:**

Both `ProcessLogic` and `ConsolidateUtxos` tasks acquire the same `TimerLogicGuard` (single `is_timer_running` flag): [6](#0-5) 

This prevents concurrent execution but not sequential execution. `ConsolidateUtxos` runs, depletes `available_utxos`, releases the guard, and then `ProcessLogic` runs against the depleted pool. The two tasks are separate entries in the task queue and fire in separate timer invocations: [7](#0-6) 

**Both fields coexist in `CkBtcMinterState` but are never cross-checked:** [8](#0-7) 

## Impact Explanation

A user calls `retrieve_btc` or `retrieve_btc_with_approval`, which burns ckBTC on the ledger — an irreversible on-chain action: [9](#0-8) 

If UTXO consolidation fires before the withdrawal is processed and removes enough value from `available_utxos`, the withdrawal is re-queued and cannot be fulfilled until the consolidation Bitcoin transaction receives `min_confirmations` confirmations. During this window the user holds neither ckBTC nor BTC. This constitutes concrete, measurable user harm in an in-scope Chain Fusion / ck-token financial integration, qualifying as **High** severity: "Significant Chain Fusion, ck-token, ledger... security impact with concrete user or protocol harm."

## Likelihood Explanation

- On mainnet the minter currently holds tens of thousands of UTXOs, so the `utxo_consolidation_threshold` is routinely exceeded and consolidation fires every `MIN_CONSOLIDATION_INTERVAL`.
- The `ConsolidateUtxos` task runs on a 1-hour interval independently of `ProcessLogic` (5-second interval); any user withdrawal submitted between two timer ticks is at risk.
- The upgrade notes for the consolidation feature explicitly acknowledge the tension between consolidation and parallel withdrawal serving: [10](#0-9) 
- The integration test `test_utxo_consolidation_multiple` confirms that each consolidation round measurably reduces `ckbtc_minter_utxos_available` by `MAX_NUM_INPUTS_IN_TRANSACTION - 2`, demonstrating the depletion is real and quantifiable: [11](#0-10) 

## Recommendation

Before calling `select_utxos_to_consolidate()`, compute the total satoshi value required by all `pending_retrieve_btc_requests` and abort consolidation if the remaining `available_utxos` value after removing consolidation inputs would be insufficient to cover those requests:

```rust
// Inside consolidate_utxos(), before select_utxos_to_consolidate():
let pending_amount: u64 = read_state(|s| {
    s.pending_retrieve_btc_requests.iter().map(|r| r.amount).sum()
});
let available_value: u64 = read_state(|s| {
    s.available_utxos.iter().map(|u| u.value).sum()
});
if available_value.saturating_sub(pending_amount) < MIN_CONSOLIDATION_VALUE {
    return Err(ConsolidateUtxosError::InsufficientLiquidityForPendingWithdrawals);
}
```

Alternatively, add a guard that skips consolidation entirely when `pending_retrieve_btc_requests` is non-empty, or prioritize `ProcessLogic` over `ConsolidateUtxos` in the task scheduler so pending withdrawals are always drained before consolidation is attempted.

## Proof of Concept

1. Ensure the minter holds N UTXOs where N ≥ `utxo_consolidation_threshold` (e.g., N = 10,001 with default threshold 10,000).
2. User calls `retrieve_btc(amount = total_available_value - epsilon)` → ckBTC burned; request enters `pending_retrieve_btc_requests`.
3. `ConsolidateUtxos` timer fires → passes all three guards → `select_utxos_to_consolidate` removes `max_num_inputs_in_transaction` (e.g., 1,000) smallest UTXOs from `available_utxos`.
4. `ProcessLogic` timer fires → `submit_pending_requests` → `build_batch` → `available_utxos_value < req.amount` → request re-queued.
5. User's withdrawal is stuck until the consolidation Bitcoin transaction receives `min_confirmations` confirmations (minutes to days depending on mempool conditions).

A deterministic integration test can reproduce this by: (a) depositing UTXOs above the threshold, (b) submitting a withdrawal for nearly the full available value, (c) triggering `ConsolidateUtxos` via `advance_time(MIN_CONSOLIDATION_INTERVAL)` and ticking, (d) asserting that `submit_pending_requests` leaves the request in `pending_retrieve_btc_requests` with `available_utxos` insufficient to cover it.

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L1434-1453)
```rust
    let utxo_consolidation_threshold = read_state(|s| s.utxo_consolidation_threshold);

    // Return early if number of available UTXOs is below consolidation threshold.
    if read_state(|s| s.available_utxos.len() < utxo_consolidation_threshold) {
        return Err(ConsolidateUtxosError::TooFewAvailableUtxos);
    }

    // Return early if MIN_CONSOLIDATION_INTERVAL is not met since last submission.
    let now = runtime.time();
    let last_submission = read_state(|s| s.last_consolidate_utxos_request_time_ns);
    if Timestamp::new(now).checked_duration_since(Timestamp::new(last_submission))
        < Some(MIN_CONSOLIDATION_INTERVAL)
    {
        return Err(ConsolidateUtxosError::TooSoon);
    }

    // Return early if there is still an on-going transaction.
    if read_state(|s| s.current_consolidate_utxos_request.is_some()) {
        return Err(ConsolidateUtxosError::StillProcessing);
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L1455-1457)
```rust
    let input_utxos = mutate_state(|s| {
        select_utxos_to_consolidate(&mut s.available_utxos, s.max_num_inputs_in_transaction)
    });
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L1550-1561)
```rust
// Return UTXOs for consolidation and remove them from available_utxos.
fn select_utxos_to_consolidate(available_utxos: &mut UtxoSet, num_inputs: usize) -> Vec<Utxo> {
    let mut utxos = Vec::with_capacity(num_inputs);
    while utxos.len() < num_inputs {
        if let Some(utxo) = available_utxos.pop_first() {
            utxos.push(utxo);
        } else {
            break;
        }
    }
    utxos
}
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L459-484)
```rust
    /// Retrieve_btc requests that are waiting to be served, sorted by received_at.
    pub pending_retrieve_btc_requests: Vec<RetrieveBtcRequest>,

    /// Maps Account to its retrieve_btc requests burn block indices.
    pub retrieve_btc_account_to_block_indices: BTreeMap<Account, Vec<u64>>,

    /// The identifiers of retrieve_btc requests which we're currently signing a
    /// transaction or sending to the Bitcoin network.
    pub requests_in_flight: BTreeMap<u64, InFlightStatus>,

    /// Last transaction submission timestamp.
    pub last_transaction_submission_time_ns: Option<u64>,

    /// The created time of the last ConsolidateUtxosRequest.
    // This is needed in addition to `current_consolidate_utxos_request` because the latter may be
    // removed, but its time has to be remembered.
    pub last_consolidate_utxos_request_time_ns: u64,

    /// Current consolidateUtxos request that has not yet finalized.
    pub current_consolidate_utxos_request: Option<ConsolidateUtxosRequest>,

    /// Minimum number of available UTXOs to trigger a consolidation.
    pub utxo_consolidation_threshold: usize,

    /// The maximum number of input UTXOs allowed in a transaction.
    pub max_num_inputs_in_transaction: usize,
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L943-955)
```rust
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
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L1068-1080)
```rust
            SubmittedWithdrawalRequests::ToConsolidate { request } => {
                assert_eq!(
                    self.current_consolidate_utxos_request.as_ref(),
                    Some(&request)
                );
                self.current_consolidate_utxos_request = None;
                self.push_finalized_request(FinalizedBtcRequest {
                    request: request.into(),
                    state: FinalizedStatus::Confirmed { txid: *txid },
                });
                self.cleanup_tx_replacement_chain(txid);
                None
            }
```

**File:** rs/bitcoin/ckbtc/minter/src/guard.rs (L70-90)
```rust
pub struct TimerLogicGuard(());

impl TimerLogicGuard {
    pub fn new() -> Option<Self> {
        mutate_state(|s| {
            if s.is_timer_running {
                return None;
            }
            s.is_timer_running = true;
            Some(TimerLogicGuard(()))
        })
    }
}

impl Drop for TimerLogicGuard {
    fn drop(&mut self) {
        mutate_state(|s| {
            s.is_timer_running = false;
        });
    }
}
```

**File:** rs/bitcoin/ckbtc/minter/src/tasks.rs (L134-191)
```rust
pub(crate) async fn run_task<R: CanisterRuntime>(task: Task, runtime: R) {
    match task.task_type {
        TaskType::ProcessLogic => {
            const INTERVAL_PROCESSING: Duration = Duration::from_secs(5);

            let _enqueue_followup_guard = guard((), |_| {
                schedule_after(INTERVAL_PROCESSING, TaskType::ProcessLogic, &runtime)
            });

            let _guard = match crate::guard::TimerLogicGuard::new() {
                Some(guard) => guard,
                None => return,
            };

            submit_pending_requests(&runtime).await;
            finalize_requests(&runtime).await;
            reimburse_withdrawals(&runtime).await;
        }
        TaskType::RefreshFeePercentiles => {
            let _enqueue_followup_guard = guard((), |_| {
                schedule_after(
                    runtime.refresh_fee_percentiles_frequency(),
                    TaskType::RefreshFeePercentiles,
                    &runtime,
                )
            });

            let _guard = match crate::guard::TimerLogicGuard::new() {
                Some(guard) => guard,
                None => return,
            };
            let _ = estimate_fee_per_vbyte(&runtime).await;
        }
        TaskType::ConsolidateUtxos => {
            const CONSOLIDATION_TASK_INTERVAL: Duration = Duration::from_secs(3600);

            let _enqueue_followup_guard = guard((), |_| {
                schedule_after(
                    CONSOLIDATION_TASK_INTERVAL,
                    TaskType::ConsolidateUtxos,
                    &runtime,
                )
            });

            let _guard = match crate::guard::TimerLogicGuard::new() {
                Some(guard) => guard,
                None => return,
            };
            let result = consolidate_utxos(&runtime).await;
            // This is a low frequency log
            canlog::log!(
                crate::logs::Priority::Info,
                "[run_task] consolidate_utxos returns {:?}",
                result
            );
        }
    }
}
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L314-333)
```rust
    let block_index = burn_ckbtcs_icrc2(
        caller_account,
        args.amount,
        crate::memo::encode(&burn_memo_icrc2).into(),
    )
    .await?;

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

    mutate_state(|s| state::audit::accept_retrieve_btc_request(s, request, runtime));
```

**File:** rs/bitcoin/ckbtc/mainnet/minter_upgrade_2025_12_12.md (L22-23)
```markdown
 * On the other hand, the minter needs to have sufficiently many UTXOs to be able to serve multiple withdrawal requests in parallel.
 * Simulations have shown that the sweet spot for the minter is around 10k UTXOs.
```

**File:** rs/bitcoin/ckbtc/minter/tests/tests.rs (L2362-2368)
```rust
        let new_count = COUNT - (MAX_NUM_INPUTS_IN_TRANSACTION - 2) * i as usize;
        ckbtc
            .check_minter_metrics()
            .assert_contains_metric_matching(format!(
                "ckbtc_minter_utxos_available {new_count} \\d+"
            ));
        ckbtc.env.advance_time(MIN_CONSOLIDATION_INTERVAL);
```
