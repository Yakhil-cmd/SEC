### Title
Linear Increase in ckETH Withdrawal Processing Wait Time Due to Fixed Batch Size of 5 Per Timer Cycle — (File: `rs/ethereum/cketh/minter/src/withdraw.rs`)

---

### Summary
The ckETH minter's withdrawal processing pipeline is hard-capped at 5 requests per processing cycle (`WITHDRAWAL_REQUESTS_BATCH_SIZE = 5`). With up to 100 pending requests allowed (`MAX_PENDING = 100`), the last request in a saturated queue waits approximately 57+ minutes just for Ethereum transaction creation — a linear increase in wait time proportional to queue depth. The signing and sending stages are identically capped, compounding the delay further.

---

### Finding Description

In `rs/ethereum/cketh/minter/src/withdraw.rs`, three hard-coded batch-size constants govern the entire withdrawal pipeline:

```rust
const WITHDRAWAL_REQUESTS_BATCH_SIZE: usize = 5;
const TRANSACTIONS_TO_SIGN_BATCH_SIZE: usize = 5;
const TRANSACTIONS_TO_SEND_BATCH_SIZE: usize = 5;
``` [1](#0-0) 

`process_retrieve_eth_requests()` is the single entry point for all withdrawal processing. It calls each pipeline stage sequentially:

```rust
create_transactions_batch(gas_fee_estimate);   // creates ≤5 txs
sign_transactions_batch().await;               // signs  ≤5 txs
send_transactions_batch(latest_transaction_count).await;  // sends ≤5 txs
finalize_transactions_batch().await;
``` [2](#0-1) 

`create_transactions_batch()` explicitly limits itself to `WITHDRAWAL_REQUESTS_BATCH_SIZE`:

```rust
fn create_transactions_batch(gas_fee_estimate: GasFeeEstimate) {
    for request in read_state(|s| {
        s.eth_transactions
            .withdrawal_requests_batch(WITHDRAWAL_REQUESTS_BATCH_SIZE)
    }) {
``` [3](#0-2) 

`withdrawal_requests_batch()` enforces this cap:

```rust
pub fn withdrawal_requests_batch(&self, requested_batch_size: usize) -> Vec<WithdrawalRequest> {
    const MAX_NUM_PENDING_TRANSACTION_NONCES: usize = 1000;
    ...
    let actual_batch_size = min(
        MAX_NUM_PENDING_TRANSACTION_NONCES
            .saturating_sub(unique_pending_transaction_nonces.len()),
        requested_batch_size,
    );
    self.withdrawal_requests_iter()
        .take(actual_batch_size)
        .cloned()
        .collect()
}
``` [4](#0-3) 

After each cycle, if pending requests remain, a retry is scheduled:

```rust
if read_state(|s| s.eth_transactions.has_pending_requests()) {
    ic_cdk_timers::set_timer(
        crate::PROCESS_ETH_RETRIEVE_TRANSACTIONS_RETRY_INTERVAL,
        async { process_retrieve_eth_requests().await },
    );
}
``` [5](#0-4) 

The retry interval is 3 minutes and the primary interval is 6 minutes:

```rust
pub const PROCESS_ETH_RETRIEVE_TRANSACTIONS_INTERVAL: Duration = Duration::from_secs(6 * 60);
pub const PROCESS_ETH_RETRIEVE_TRANSACTIONS_RETRY_INTERVAL: Duration = Duration::from_secs(3 * 60);
``` [6](#0-5) 

The guard enforces a maximum of 100 pending withdrawal requests:

```rust
pub const MAX_CONCURRENT: usize = 100;
pub const MAX_PENDING: usize = 100;
...
if PR::pending_requests_count(s) >= MAX_PENDING {
    return Err(GuardError::TooManyPendingRequests);
}
``` [7](#0-6) [8](#0-7) 

**Wait-time arithmetic:**
With 100 pending requests and a batch size of 5, the pipeline requires 20 retry cycles to drain the creation queue. Each retry fires after 3 minutes, so the 100th request waits ≈ 19 × 3 min = **57 minutes** before its Ethereum transaction is even created. Signing (`TRANSACTIONS_TO_SIGN_BATCH_SIZE = 5`) and sending (`TRANSACTIONS_TO_SEND_BATCH_SIZE = 5`) add further linear delays on top of that. The total end-to-end wait for the last request in a saturated queue can exceed **2–3 hours**.

Additionally, `process_reimbursement()` processes every pending reimbursement sequentially in a single timer invocation — one ledger `transfer` await per entry — with no batch cap at all:

```rust
for (index, reimbursement_request) in reimbursements {
    ...
    let block_index = match client.transfer(args).await {
``` [9](#0-8) 

This is the exact "one request at a time" pattern from the referenced report.

---

### Impact Explanation
Any unprivileged IC principal can call `withdraw_eth` to queue a withdrawal. When the queue is at or near capacity (100 requests), each newly accepted request experiences a wait time that grows linearly with queue depth — up to 57+ minutes for transaction creation alone, plus additional linear delays for signing and sending. During time-sensitive market conditions or post-maturity redemption windows, this delay is directly harmful to users who have already burned their ckETH tokens and are waiting for the corresponding ETH to arrive on-chain. The funds are locked (burned) but not yet delivered.

---

### Likelihood Explanation
The `MAX_PENDING = 100` cap is reachable by 100 distinct principals each submitting one `withdraw_eth` call. During periods of high demand (e.g., ETH price volatility, protocol events), this is a realistic scenario. An adversary could also deliberately fill the queue with 100 minimum-amount withdrawals to delay other users' withdrawals, since the guard only prevents the same principal from having two concurrent in-flight requests — it does not prevent 100 different principals from each holding one slot. [10](#0-9) 

---

### Recommendation
1. **Increase batch sizes**: Raise `WITHDRAWAL_REQUESTS_BATCH_SIZE`, `TRANSACTIONS_TO_SIGN_BATCH_SIZE`, and `TRANSACTIONS_TO_SEND_BATCH_SIZE` to values commensurate with `MAX_PENDING` (e.g., 20–50) so that a full queue can be drained in 2–5 cycles rather than 20.
2. **Parallelize signing**: `sign_transactions_batch()` already uses `join_all` for parallel threshold-ECDSA calls; increasing `TRANSACTIONS_TO_SIGN_BATCH_SIZE` would directly reduce signing latency without additional architectural changes.
3. **Bound `process_reimbursement()` iterations**: Introduce a per-invocation cap on reimbursements processed, mirroring the pattern used for withdrawals, to prevent unbounded sequential ledger calls in a single timer tick.

---

### Proof of Concept

1. Using 100 distinct IC principals, each call `withdraw_eth` with the minimum withdrawal amount, filling the pending queue to `MAX_PENDING = 100`.
2. Submit a 101st withdrawal from a new principal — it is rejected with `TooManyPendingRequests`.
3. Observe via `retrieve_eth_status` that the 100th request remains in `Pending` state.
4. Measure elapsed time until the 100th request transitions to `TxCreated`: it will be approximately 57 minutes (19 retry cycles × 3 min each), confirming the linear wait-time growth.
5. Repeat with queue depths of 5, 10, 50, 100 to confirm the linear relationship between queue depth and wait time for the last enqueued request.

### Citations

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L39-41)
```rust
const WITHDRAWAL_REQUESTS_BATCH_SIZE: usize = 5;
const TRANSACTIONS_TO_SIGN_BATCH_SIZE: usize = 5;
const TRANSACTIONS_TO_SEND_BATCH_SIZE: usize = 5;
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L67-95)
```rust
    for (index, reimbursement_request) in reimbursements {
        // Ensure that even if we were to panic in the callback, after having contacted the ledger to mint the tokens,
        // this reimbursement request will not be processed again.
        let prevent_double_minting_guard = scopeguard::guard(index.clone(), |index| {
            mutate_state(|s| process_event(s, EventType::QuarantinedReimbursement { index }));
        });
        let ledger_canister_id = match index {
            ReimbursementIndex::CkEth { .. } => read_state(|s| s.cketh_ledger_id),
            ReimbursementIndex::CkErc20 { ledger_id, .. } => ledger_id,
        };
        let client = ICRC1Client {
            runtime: CdkRuntime,
            ledger_canister_id,
        };
        let memo = Memo::from(reimbursement_request.clone());
        let args = TransferArg {
            from_subaccount: None,
            to: Account {
                owner: reimbursement_request.to,
                subaccount: reimbursement_request
                    .to_subaccount
                    .map(LedgerSubaccount::to_bytes),
            },
            fee: None,
            created_at_time: None,
            memo: Some(memo),
            amount: Nat::from(reimbursement_request.reimbursed_amount),
        };
        let block_index = match client.transfer(args).await {
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L177-182)
```rust
    let latest_transaction_count = latest_transaction_count().await;
    resubmit_transactions_batch(latest_transaction_count, &gas_fee_estimate).await;
    create_transactions_batch(gas_fee_estimate);
    sign_transactions_batch().await;
    send_transactions_batch(latest_transaction_count).await;
    finalize_transactions_batch().await;
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L184-189)
```rust
    if read_state(|s| s.eth_transactions.has_pending_requests()) {
        ic_cdk_timers::set_timer(
            crate::PROCESS_ETH_RETRIEVE_TRANSACTIONS_RETRY_INTERVAL,
            async { process_retrieve_eth_requests().await },
        );
    }
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L249-253)
```rust
fn create_transactions_batch(gas_fee_estimate: GasFeeEstimate) {
    for request in read_state(|s| {
        s.eth_transactions
            .withdrawal_requests_batch(WITHDRAWAL_REQUESTS_BATCH_SIZE)
    }) {
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L906-923)
```rust
    pub fn withdrawal_requests_batch(&self, requested_batch_size: usize) -> Vec<WithdrawalRequest> {
        // The number of pending transaction nonces is counted and not the number of pending transactions
        // because a nonce may be associated with several distinct transactions (due to re-submission and dynamic fees).
        // However, once a nonce is chosen for a withdrawal request, it's in our interest that the corresponding transaction be finalized asap.
        // Limiting the number of transactions would be counter-productive.
        const MAX_NUM_PENDING_TRANSACTION_NONCES: usize = 1000;
        let unique_pending_transaction_nonces: BTreeSet<_> =
            self.created_tx.keys().chain(self.sent_tx.keys()).collect();
        let actual_batch_size = min(
            MAX_NUM_PENDING_TRANSACTION_NONCES
                .saturating_sub(unique_pending_transaction_nonces.len()),
            requested_batch_size,
        );
        self.withdrawal_requests_iter()
            .take(actual_batch_size)
            .cloned()
            .collect()
    }
```

**File:** rs/ethereum/cketh/minter/src/lib.rs (L35-37)
```rust
pub const PROCESS_ETH_RETRIEVE_TRANSACTIONS_INTERVAL: Duration = Duration::from_secs(6 * 60);
pub const PROCESS_REIMBURSEMENT: Duration = Duration::from_secs(3 * 60);
pub const PROCESS_ETH_RETRIEVE_TRANSACTIONS_RETRY_INTERVAL: Duration = Duration::from_secs(3 * 60);
```

**File:** rs/ethereum/cketh/minter/src/guard/mod.rs (L9-10)
```rust
pub const MAX_CONCURRENT: usize = 100;
pub const MAX_PENDING: usize = 100;
```

**File:** rs/ethereum/cketh/minter/src/guard/mod.rs (L46-68)
```rust
impl<PR: RequestsGuardedByPrincipal> Guard<PR> {
    /// Attempts to create a new guard for the current code block. Fails if there is
    /// already a pending request for the specified [principal] or if there
    /// are at least [MAX_CONCURRENT] pending requests.
    fn new(principal: Principal) -> Result<Self, GuardError> {
        mutate_state(|s| {
            if PR::pending_requests_count(s) >= MAX_PENDING {
                return Err(GuardError::TooManyPendingRequests);
            }
            let principals = PR::guarded_principals(s);
            if principals.contains(&principal) {
                return Err(GuardError::AlreadyProcessing);
            }
            if principals.len() >= MAX_CONCURRENT {
                return Err(GuardError::TooManyConcurrentRequests);
            }
            principals.insert(principal);
            Ok(Self {
                principal,
                _marker: PhantomData,
            })
        })
    }
```
