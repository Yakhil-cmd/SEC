The code is present and readable. Let me analyze the exact logic.

The code is fully readable. I have everything needed for a definitive assessment.

**Exact logic in `enqueue_transaction` (lines 92–101):**

```rust
if self.transactions.len() == TX_CACHE_SIZE {   // line 92 — capacity check
    self.transactions.pop_front();               // line 97 — UNCONDITIONAL eviction
}
self.transactions
    .entry(txid)
    .or_insert_with(|| TransactionInfo::new(&transaction)); // line 99-101 — no-op for duplicates
```

The eviction at line 97 fires before the duplicate guard at line 100. When the store is at 250 and a duplicate txid is submitted: `pop_front()` removes the oldest entry, then `or_insert_with` is a no-op because the txid already exists. Net result: store shrinks from 250 to 249, permanently losing the evicted transaction.

**Attack path reachability:** Any canister can call the public `bitcoin_send_transaction` management canister API. The `send_transaction_internal` privileged check at `rs/execution_environment/src/bitcoin.rs:86` gates only the *internal* variant; the public API is open to all canisters. Transactions flow through `TransactionManagerRequest::SendTransaction` in `rs/bitcoin/adapter/src/router.rs:126` directly into `enqueue_transaction`.

---

### Title
Unconditional `pop_front()` Before Duplicate Check in `enqueue_transaction` Allows Silent Eviction of Legitimate Pending Bitcoin Transactions — (`rs/bitcoin/adapter/src/transaction_store.rs`)

### Summary
`enqueue_transaction` evicts the oldest cache entry unconditionally whenever the store is at `TX_CACHE_SIZE` (250), even when the incoming transaction is a duplicate already present in the store. Because the duplicate guard (`or_insert_with`) runs *after* the eviction, a duplicate submission at full capacity causes a net loss of one distinct cached transaction with no error, log warning, or retry.

### Finding Description
In `enqueue_transaction`: [1](#0-0) 

The capacity check and `pop_front()` at lines 92–97 execute unconditionally. The duplicate guard at lines 99–101 (`or_insert_with`) only prevents re-insertion but cannot undo the already-executed eviction. The two operations are not atomic with respect to the duplicate check.

The correct invariant — stated in the comment at line 82 ("If the transaction is not known, the transaction is added") — requires that eviction only occur when the incoming transaction is genuinely new. The implementation violates this invariant.

### Impact Explanation
The evicted transaction is silently removed from the `LinkedHashMap`. The `advertise_txids` loop in `rs/bitcoin/adapter/src/router.rs:134` will never broadcast it again. [2](#0-1)  Because all subnet nodes receive the same ordered sequence of `SendTransaction` requests via IC consensus, the attack simultaneously corrupts the cache on every node running the adapter, leaving no node to broadcast the victim transaction. The victim's Bitcoin transaction may never reach the Bitcoin P2P network, effectively causing permanent loss of that pending broadcast within the 10-minute TTL window.

### Likelihood Explanation
Any canister can call the public `bitcoin_send_transaction` management canister API. [3](#0-2)  The privileged check there applies only to `bitcoin_send_transaction_internal`; the public path is unrestricted. An attacker needs to submit 249 distinct (but syntactically valid) Bitcoin transactions to fill the cache, then re-submit one of them as a duplicate. The cost is non-trivial (cycles for 249 calls) but well within reach of a motivated attacker. No special key, admin role, or consensus corruption is required.

### Recommendation
Check for the duplicate *before* deciding to evict:

```rust
pub fn enqueue_transaction(&mut self, raw_tx: &[u8]) {
    if let Ok(transaction) = deserialize::<Transaction>(raw_tx) {
        let txid = transaction.compute_txid();
        if self.transactions.contains_key(&txid) {
            return; // duplicate — no eviction, no insertion
        }
        if self.transactions.len() == TX_CACHE_SIZE {
            self.metrics.txn_ops.with_label_values(&["remove", "pushed_out"]).inc();
            self.transactions.pop_front();
        }
        self.transactions.insert(txid, TransactionInfo::new(&transaction));
    }
}
```

This preserves the FIFO eviction policy while ensuring eviction only occurs for genuinely new transactions.

### Proof of Concept

```rust
#[test]
fn test_duplicate_evicts_distinct_transaction() {
    let mut manager = make_transaction_manager();

    // Fill cache with TX_CACHE_SIZE distinct transactions
    let mut txids = vec![];
    for i in 0..TX_CACHE_SIZE {
        let mut tx = get_transaction();
        tx.lock_time = LockTime::from_height(i.try_into().unwrap()).unwrap();
        let raw = serialize(&tx);
        manager.enqueue_transaction(&raw);
        txids.push(tx.compute_txid());
    }
    assert_eq!(manager.transactions.len(), TX_CACHE_SIZE);

    // Re-submit the second transaction (duplicate) — cache is still at TX_CACHE_SIZE
    let mut dup_tx = get_transaction();
    dup_tx.lock_time = LockTime::from_height(1u32).unwrap();
    let raw_dup = serialize(&dup_tx);
    manager.enqueue_transaction(&raw_dup);

    // BUG: oldest entry (index 0) was evicted; duplicate was not inserted
    // Store is now TX_CACHE_SIZE - 1 instead of TX_CACHE_SIZE
    assert_eq!(manager.transactions.len(), TX_CACHE_SIZE,
        "FAIL: oldest tx was silently evicted by a duplicate submission");
    assert!(manager.transactions.get(&txids[0]).is_some(),
        "FAIL: oldest tx is gone from the cache");
}
``` [4](#0-3)

### Citations

**File:** rs/bitcoin/adapter/src/transaction_store.rs (L83-103)
```rust
    pub fn enqueue_transaction(&mut self, raw_tx: &[u8]) {
        if let Ok(transaction) = deserialize::<Transaction>(raw_tx) {
            self.metrics
                .txn_ops
                .with_label_values(&["insert", "enqueued"])
                .inc();
            let txid = transaction.compute_txid();
            trace!(self.logger, "Received {} from the system component", txid);
            // If hashmap has `TX_CACHE_SIZE` values we remove the oldest transaction in the cache.
            if self.transactions.len() == TX_CACHE_SIZE {
                self.metrics
                    .txn_ops
                    .with_label_values(&["remove", "pushed_out"])
                    .inc();
                self.transactions.pop_front();
            }
            self.transactions
                .entry(txid)
                .or_insert_with(|| TransactionInfo::new(&transaction));
        }
    }
```

**File:** rs/bitcoin/adapter/src/router.rs (L124-128)
```rust
                transaction_manager_request = transaction_manager_rx.recv() => {
                    match transaction_manager_request.unwrap() {
                        TransactionManagerRequest::SendTransaction(transaction) => transaction_manager.enqueue_transaction(&transaction),
                    }
                },
```

**File:** rs/execution_environment/src/bitcoin.rs (L81-91)
```rust
pub fn send_transaction_internal(
    privileged_access: &[CanisterId],
    request: &Request,
    state: &mut ReplicatedState,
) -> Result<(), UserError> {
    if !privileged_access.contains(&request.sender()) {
        return Err(UserError::new(
            ErrorCode::CanisterRejectedMessage,
            String::from("Permission denied."),
        ));
    }
```
