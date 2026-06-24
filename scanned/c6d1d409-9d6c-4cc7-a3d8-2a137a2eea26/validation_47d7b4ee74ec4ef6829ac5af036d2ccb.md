### Title
Unbounded O(n) Per-Round Scan in `purge_expired_ingress_messages` Enables Replica CPU DoS via Ingress Queue Flooding — (File: `rs/execution_environment/src/scheduler.rs`)

---

### Summary

The `IngressQueue` data structure explicitly carries no upper bound on the number of messages it can hold per canister. The scheduler's `purge_expired_ingress_messages` function, invoked unconditionally at the start of every consensus round, performs an O(n) linear scan over all ingress messages for every hot canister and over the subnet's own ingress queue. An unprivileged ingress sender can flood a target canister's ingress queue with many small messages (all set to the maximum TTL), causing O(n) CPU work in the replica's native execution path every round for the entire TTL window, and a second O(n) burst of `set_status` writes when the messages expire.

---

### Finding Description

**Root cause — no per-canister ingress queue bound:**

The `IngressQueue` struct is explicitly documented as having no upper bound:

```
/// Representation of the Ingress queue. There is no upper bound on
/// the number of messages it can store.
``` [1](#0-0) 

The struct holds a `BTreeMap<Option<CanisterId>, VecDeque<Arc<Ingress>>>` with no capacity check in `push()`: [2](#0-1) 

**O(n) per-round scan path — `purge_expired_ingress_messages`:**

Every round, before any canister execution, the scheduler calls:

```rust
fn purge_expired_ingress_messages(...) {
    let not_expired = |ingress: &Ingress| ingress.expiry_time >= current_time;
    let mut expired_ingress_messages = state.subnet_queues_retain_ingress_messages(not_expired); // O(n) unconditional
    for canister in state.hot_canisters_iter_mut() {
        if !canister.system_state.all_ingress_messages(not_expired) {          // O(n) per canister
            ...
            expired_ingress_messages.extend(canister.system_state.retain_ingress_messages(not_expired)); // O(n)
        }
    }
    for ingress in expired_ingress_messages.iter() {
        self.ingress_history_writer.set_status(...);  // O(n) writes to ingress history BTreeMap
    }
}
``` [3](#0-2) 

This is called unconditionally at the start of every round: [4](#0-3) 

**Two distinct O(n) phases:**

1. **Every round (pre-expiry):** `all_ingress_messages(not_expired)` scans all messages for every hot canister to check whether any are expired. If the attacker has flooded a canister with N messages all set to expire in the future, this predicate scans all N messages and returns `true` — O(N) work per round, every round, for the entire TTL window. [5](#0-4) 

2. **At expiry (one-time burst):** `retain_messages` performs an O(N) `retain_mut` over the per-canister `VecDeque` and an O(N) `retain_mut` over the `schedule` `VecDeque`, followed by one `set_status` call per expired message (each a `BTreeMap` write — O(log M) where M is ingress history size). [6](#0-5) 

**Subnet ingress queue — unconditional O(n) every round:**

The subnet's own ingress queue (for management-canister calls) is scanned via `subnet_queues_retain_ingress_messages` with **no** `all_ingress_messages` guard — it always calls `retain_messages` regardless of whether any messages are expired: [7](#0-6) 

Subnet management messages are inducted **free of charge** (no cycles deducted): [8](#0-7) 

This means an attacker can flood the subnet's ingress queue with zero-cost messages, causing unconditional O(n) `retain_messages` work every round.

---

### Impact Explanation

The O(n) work occurs in the replica's native Rust execution path, outside of any Wasm instruction budget. It directly consumes wall-clock CPU time in the round-preparation phase. With `MAX_INGRESS_TTL = 5 minutes` and `max_ingress_messages_per_block = 1000`, up to ~300,000 messages can accumulate: [9](#0-8) 

Each round, the scheduler must scan all N accumulated messages before executing any canister. If this scan is slow enough to push the round past its time budget, block production stalls and the subnet falls behind consensus. At expiry, the burst of `set_status` writes to the ingress history `BTreeMap` amplifies the cost further. The `ingress_history_max_messages` limit is bypassed for system subnets: [10](#0-9) 

---

### Likelihood Explanation

An unprivileged user with internet access to any boundary node can submit ingress messages. Subnet management calls (e.g., `canister_status`, `raw_rand`) are free and accepted from any principal. The attacker needs only to sustain ~1000 messages/block for ~5 minutes to fill the queue to its practical maximum. No privileged access, key material, or governance majority is required. The `IngressPoolThrottler` limits per-node pool size but does not prevent messages from being included in blocks by other nodes: [11](#0-10) [12](#0-11) 

---

### Recommendation

1. **Add a per-canister ingress queue depth limit** in `IngressQueue::push()`, analogous to `DEFAULT_QUEUE_CAPACITY = 500` used for canister-to-canister queues. [13](#0-12) 

2. **Guard `subnet_queues_retain_ingress_messages`** with an `all_ingress_messages` pre-check (as is done for per-canister queues) to avoid the unconditional O(n) scan when no messages are expired.

3. **Bound the number of free subnet-addressed ingress messages** per sender per block to prevent zero-cost flooding of the subnet ingress queue.

---

### Proof of Concept

```
Attacker (any principal):
  for i in 0..300_000:
    submit ingress to IC management canister (e.g., canister_status)
    with expiry_time = now + MAX_INGRESS_TTL (5 min)
    // free of charge; accepted by boundary node

Effect per round (every ~1 second for 5 minutes):
  purge_expired_ingress_messages():
    subnet_queues_retain_ingress_messages(not_expired)
      -> IngressQueue::retain_messages() scans all 300,000 messages  // O(300K) per round
    for each hot canister:
      all_ingress_messages(not_expired)
        -> scans all N messages per canister                          // O(N) per canister per round

Effect at T+5min (one-time burst):
  retain_messages() removes 300,000 entries from VecDeque            // O(300K)
  set_status() called 300,000 times -> BTreeMap writes               // O(300K * log(600K))
```

### Citations

**File:** rs/replicated_state/src/canister_state/queues/queue.rs (L294-295)
```rust
/// Representation of the Ingress queue. There is no upper bound on
/// the number of messages it can store.
```

**File:** rs/replicated_state/src/canister_state/queues/queue.rs (L334-349)
```rust
    pub(super) fn push(&mut self, msg: Ingress) {
        let msg_size = Self::ingress_size_bytes(&msg);
        let receiver_ingress_queue = self.queues.entry(msg.effective_canister_id).or_default();

        if receiver_ingress_queue.is_empty() {
            self.schedule.push_back(msg.effective_canister_id);
            self.size_bytes += Self::PER_CANISTER_QUEUE_OVERHEAD_BYTES;
        }

        receiver_ingress_queue.push_back(Arc::new(msg));

        self.size_bytes += msg_size;
        debug_assert_eq!(Self::size_bytes(&self.queues), self.size_bytes);

        self.total_ingress_count += 1;
    }
```

**File:** rs/replicated_state/src/canister_state/queues/queue.rs (L413-420)
```rust
    pub(super) fn all_messages<F>(&self, mut predicate: F) -> bool
    where
        F: FnMut(&Ingress) -> bool,
    {
        self.queues
            .values()
            .all(|queue| queue.iter().all(|msg| predicate(msg)))
    }
```

**File:** rs/replicated_state/src/canister_state/queues/queue.rs (L424-455)
```rust
    pub(super) fn retain_messages<F>(&mut self, mut predicate: F) -> Vec<Arc<Ingress>>
    where
        F: FnMut(&Ingress) -> bool,
    {
        let mut filtered_messages = vec![];
        for canister_ingress_queue in self.queues.values_mut() {
            canister_ingress_queue.retain_mut(|item| {
                if predicate(item) {
                    return true;
                }
                // Empty `canister_ingress_queues` and their corresponding schedule entry
                // are pruned below.
                filtered_messages.push(Arc::clone(item));
                self.size_bytes -= Self::ingress_size_bytes(&(*item));
                self.total_ingress_count -= 1;
                false
            });
        }

        self.schedule
            .retain_mut(|canister_id| match self.queues.entry(*canister_id) {
                Entry::Occupied(entry) if entry.get().is_empty() => {
                    entry.remove();
                    self.size_bytes -= Self::PER_CANISTER_QUEUE_OVERHEAD_BYTES;
                    false
                }
                Entry::Occupied(_) => true,
                Entry::Vacant(_) => unreachable!(),
            });

        filtered_messages
    }
```

**File:** rs/execution_environment/src/scheduler.rs (L772-812)
```rust
    fn purge_expired_ingress_messages(
        &self,
        state: &mut ReplicatedState,
        canister_ingress_latencies: &mut CanisterIngressQueueLatencies,
        current_round: ExecutionRound,
    ) {
        let current_time = state.time();
        let not_expired = |ingress: &Ingress| ingress.expiry_time >= current_time;
        let mut expired_ingress_messages = state.subnet_queues_retain_ingress_messages(not_expired);
        for canister in state.hot_canisters_iter_mut() {
            if !canister.system_state.all_ingress_messages(not_expired) {
                let canister = Arc::make_mut(canister);
                expired_ingress_messages
                    .extend(canister.system_state.retain_ingress_messages(not_expired));
            }
        }
        self.metrics
            .expired_ingress_messages_count
            .inc_by(expired_ingress_messages.len() as u64);
        for ingress in expired_ingress_messages.iter() {
            let error = UserError::new(
                ErrorCode::IngressMessageTimeout,
                format!(
                    "Ingress message {} timed out waiting to start executing.",
                    ingress.message_id
                ),
            );
            let old_status = self.ingress_history_writer.set_status(
                state,
                ingress.message_id.clone(),
                IngressStatus::Known {
                    receiver: ingress.receiver.get(),
                    user_id: ingress.source,
                    time: current_time,
                    state: IngressState::Failed(error),
                },
                current_round,
            );
            canister_ingress_latencies.on_ingress_status_changed(&old_status);
        }
    }
```

**File:** rs/execution_environment/src/scheduler.rs (L1224-1230)
```rust
                let _timer = self.metrics.round_preparation_ingress.start_timer();
                self.purge_expired_ingress_messages(
                    &mut state,
                    &mut canister_ingress_latencies,
                    current_round,
                );
            }
```

**File:** rs/replicated_state/src/replicated_state.rs (L1161-1166)
```rust
    pub fn subnet_queues_retain_ingress_messages<F>(&mut self, predicate: F) -> Vec<Arc<Ingress>>
    where
        F: FnMut(&Ingress) -> bool,
    {
        self.subnet_queues.retain_ingress_messages(predicate)
    }
```

**File:** rs/messaging/src/scheduling/valid_set_rule.rs (L250-256)
```rust
        if state.metadata.own_subnet_type != SubnetType::System
            && state.metadata.ingress_history.len() >= self.ingress_history_max_messages
        {
            return Err(IngressInductionError::IngressHistoryFull {
                capacity: self.ingress_history_max_messages,
            });
        }
```

**File:** rs/messaging/src/scheduling/valid_set_rule.rs (L286-291)
```rust
        match induction_cost {
            IngressInductionCost::Free => {
                // Only subnet methods can be free. These are enqueued directly.
                assert!(ingress.is_addressed_to_subnet());
                state.push_ingress(ingress)
            }
```

**File:** rs/limits/src/lib.rs (L29-37)
```rust
/// The maximum number of messages that can be present in the ingress history
/// at any one time.
///
/// The value is the product of the default `max_ingress_messages_per_block`
/// configured in the subnet record; and the `MAX_INGRESS_TTL` (assuming a block
/// rate of 1 block per second). Times 2, since we could theoretically have
/// `MAX_INGRESS_TTL` worth of `Received` messages; plus the same number of
/// messages in terminal states.
pub const INGRESS_HISTORY_MAX_MESSAGES: usize = 2 * 1000 * MAX_INGRESS_TTL.as_secs() as usize;
```

**File:** rs/http_endpoints/public/src/call.rs (L229-236)
```rust
        // Load shed the request if the ingress pool is full.
        let ingress_pool_is_full = ingress_throttler.read().unwrap().exceeds_threshold();
        if ingress_pool_is_full {
            Err(HttpError {
                status: StatusCode::SERVICE_UNAVAILABLE,
                message: "Service is overloaded, try again later.".to_string(),
            })?;
        }
```

**File:** rs/artifact_pool/src/ingress_pool.rs (L226-232)
```rust
    fn exceeds_limit(&self, peer_id: &NodeId) -> bool {
        let counters = self.unvalidated.peer_counters.get_counters(peer_id)
            + self.validated.peer_counters.get_counters(peer_id);

        counters.bytes > self.ingress_pool_max_bytes
            || counters.messages > self.ingress_pool_max_count
    }
```

**File:** rs/replicated_state/src/canister_state/queues.rs (L41-41)
```rust
pub const DEFAULT_QUEUE_CAPACITY: usize = 500;
```
