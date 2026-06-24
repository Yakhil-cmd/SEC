Looking at the actual code, I need to trace the full path and check for any guards.

**Key code facts:**

`QuerySchedulerCore::push` in `internal.rs`: [1](#0-0) 

The `entry().or_insert_with(CanisterData::new)` call unconditionally inserts a new `CanisterData` for every unseen `canister_id`. No eviction exists anywhere in `pop`, `notify_finished_execution`, or any other method. [2](#0-1) [3](#0-2) 

**The insertion happens before canister validation.** In `HttpQueryHandler::call`, `query_scheduler.push(canister_id, closure)` is called immediately with the raw `query.receiver`, and the canister existence check (`CanisterNotFound`) only happens *inside* the closure, which runs later on a worker thread: [4](#0-3) 

**No upstream guard prevents this.** The `QueryScheduler::push` has no size cap: [5](#0-4) 

---

### Title
Unbounded `canisters` HashMap Growth in `QuerySchedulerCore` via Arbitrary Canister IDs — (`rs/execution_environment/src/query_handler/query_scheduler/internal.rs`)

### Summary
`QuerySchedulerCore::canisters` accumulates one `CanisterData` entry per unique `CanisterId` ever seen and never evicts them. Because the canister-existence check occurs inside the deferred closure (after insertion), an unprivileged caller can drive unbounded HashMap growth by targeting distinct canister IDs.

### Finding Description
In `QuerySchedulerCore::push`, the line:
```rust
self.canisters.entry(canister_id).or_insert_with(CanisterData::new);
```
inserts a new entry for every previously-unseen `canister_id`. Neither `pop` nor `notify_finished_execution` ever removes entries from the HashMap — even after both `incoming` and `leftover` queues are empty and `active_threads == 0`. The `CanisterData` struct retains its allocation indefinitely.

The `HttpQueryHandler::call` implementation pushes to the scheduler using the raw `query.receiver` field before any canister-existence validation. Validation (`CanisterNotFound`) only fires inside the worker-thread closure, after the HashMap entry is already created. [6](#0-5) 

### Impact Explanation
Each `CanisterData` entry is small (~100–200 bytes including HashMap overhead and the `CanisterId` key), but the accumulation is permanent and unbounded. At boundary-node-typical rates (e.g., 1,000 queries/s with rotating canister IDs), an attacker accumulates ~86 million entries per day (~8–17 GB). This leads to replica OOM, crashing the process and degrading subnet query availability for all users of that replica.

### Likelihood Explanation
The attack requires only standard HTTP query calls with varying `receiver` fields — no privileged access, no key material, no consensus corruption. Boundary-node rate limiting slows but does not prevent the attack because: (a) limits are typically per-IP/user, not per distinct canister ID, and (b) the memory leak is permanent — entries never expire even after the attack stops. The attack is local-testable with a simple unit test.

### Recommendation
Remove `CanisterData` entries from the HashMap when both queues are empty and `active_threads == 0`. The natural eviction points are at the end of `pop` (after draining) and `notify_finished_execution`. A one-line guard suffices:
```rust
if !canister.has_queries() && canister.active_threads == 0 {
    self.canisters.remove(&canister_id);
}
```
Alternatively, cap the HashMap size and reject `push` calls when the cap is reached, returning an error to the caller.

### Proof of Concept
```rust
// Unit test: push queries for 1M distinct canister IDs, pop and finish each,
// then assert the HashMap is empty (currently it will have 1M entries).
let scheduler = QuerySchedulerInternal::new(1, Duration::from_millis(100), &MetricsRegistry::new());
for i in 0..1_000_000u64 {
    let cid = CanisterId::from(i);
    scheduler.push(cid, Query(Box::new(|| Duration::ZERO)));
    // simulate execution finishing
    if let Some((cid, _queries)) = scheduler.try_pop() {
        scheduler.notify_finished_execution(cid, Duration::ZERO, vec![]);
    }
}
let core = scheduler.core.lock().unwrap();
assert_eq!(core.canisters.len(), 0); // FAILS: len == 1_000_000
```

### Citations

**File:** rs/execution_environment/src/query_handler/query_scheduler/internal.rs (L112-136)
```rust
struct QuerySchedulerCore {
    // Per-canister query queues and execution stats.
    canisters: HashMap<CanisterId, CanisterData>,

    // The round-robin queue of canisters.
    // Invariant: if a canister is in this queue, then:
    // - its `has_been_scheduled` flag is set.
    // - it has at least one query to execute.
    // - the number of currently running threads of this canister is below
    //   the `max_threads_per_canister` limit.
    scheduled: VecDeque<CanisterId>,

    // The limit on the number of concurrently running threads per canister.
    max_threads_per_canister: usize,

    // The time limit for executing a batch of queries.
    time_slice_per_canister: Duration,

    // This flag is set to true if tear-down was requested.
    // It is used to stop query execution threads.
    tearing_down: bool,

    // The query scheduler metrics.
    metrics: QuerySchedulerMetrics,
}
```

**File:** rs/execution_environment/src/query_handler/query_scheduler/internal.rs (L156-176)
```rust
    fn push(&mut self, canister_id: CanisterId, query: Query) {
        let canister = self
            .canisters
            .entry(canister_id)
            .or_insert_with(CanisterData::new);
        canister.incoming.push_back(query);

        self.metrics
            .queue_length
            .observe((canister.incoming.len() + canister.leftover.len()) as f64);

        if !canister.has_been_scheduled
            && canister.should_be_scheduled(self.max_threads_per_canister)
        {
            canister.has_been_scheduled = true;
            self.scheduled.push_back(canister_id);
        }

        #[cfg(debug_assertions)]
        self.verify_invariants();
    }
```

**File:** rs/execution_environment/src/query_handler/query_scheduler/internal.rs (L179-215)
```rust
    fn pop(&mut self) -> Option<(CanisterId, Vec<Query>)> {
        let canister_id = self.scheduled.pop_front()?;
        // It is safe to unwrap here because of the invariants in
        // `validate_invariants()`: each canister in the round-robin list must
        // be present in the canister table.
        let canister = self.canisters.get_mut(&canister_id).unwrap();
        debug_assert!(canister.has_been_scheduled);
        canister.has_been_scheduled = false;

        let total = canister.queries_per_time_slice(self.time_slice_per_canister);

        // Collect queries from the `leftover` queue first.
        let from_leftover = total.min(canister.leftover.len());
        let mut result: Vec<_> = canister.leftover.drain(0..from_leftover).collect();

        // Get the remaining queries from the `incoming` queue.
        let from_incoming = (total - from_leftover).min(canister.incoming.len());
        result.extend(canister.incoming.drain(0..from_incoming));

        // Follows from the main invariant.
        debug_assert!(!result.is_empty());

        canister.active_threads += 1;

        // We removed the canister from the schedule at the beginning of this
        // method and cleared `has_been_scheduled`.
        debug_assert!(!canister.has_been_scheduled);
        if canister.should_be_scheduled(self.max_threads_per_canister) {
            canister.has_been_scheduled = true;
            self.scheduled.push_back(canister_id);
        }

        #[cfg(debug_assertions)]
        self.verify_invariants();

        Some((canister_id, result))
    }
```

**File:** rs/execution_environment/src/query_handler/query_scheduler/internal.rs (L219-241)
```rust
    fn notify_finished_execution(
        &mut self,
        canister_id: CanisterId,
        average_query_duration: Duration,
        leftover: Vec<Query>,
    ) {
        let canister = self.canisters.get_mut(&canister_id).unwrap();
        canister.average_query_duration =
            (canister.average_query_duration + average_query_duration) / 2;

        canister.leftover.extend(leftover);
        canister.active_threads -= 1;

        if !canister.has_been_scheduled
            && canister.should_be_scheduled(self.max_threads_per_canister)
        {
            canister.has_been_scheduled = true;
            self.scheduled.push_back(canister_id);
        }

        #[cfg(debug_assertions)]
        self.verify_invariants();
    }
```

**File:** rs/execution_environment/src/query_handler.rs (L524-584)
```rust
        let canister_id = query.receiver;
        let latest_certified_height_pre_schedule = state_reader.latest_certified_height();
        let http_query_handler_metrics = Arc::clone(&self.metrics);
        let enable_query_stats_tracking = self.enable_query_stats_tracking;
        self.query_scheduler.push(canister_id, move || {
            let start = std::time::Instant::now();
            if !tx.is_closed() {
                // We managed to upgrade the weak pointer, so the query was not cancelled.
                // Canceling the query after this point will have no effect: the query will
                // be executed anyway. That is fine because the execution will take O(ms).

                // Retrieving the state must be done here in the query handler, and should be immediately used.
                // Otherwise, retrieving the state in the Query service in `http_endpoints` can lead to queries being queued up,
                // with a reference to older states which can cause out-of-memory crashes.

                let (certificate_delegation, certificate_delegation_metadata) =
                    match certificate_delegation_with_metadata {
                        Some((delegation, metadata)) => (Some(delegation), Some(metadata)),
                        None => (None, None),
                    };

                let result = match get_latest_certified_state_and_data_certificate(
                    state_reader,
                    certificate_delegation,
                    query.receiver,
                ) {
                    Some((state, cert)) => {
                        let time = state.get_ref().metadata.batch_time;

                        let certified_height_used_for_execution = state.height();
                        let height_diff = certified_height_used_for_execution
                            .get()
                            .saturating_sub(latest_certified_height_pre_schedule.get());
                        http_query_handler_metrics
                            .height_diff_during_query_scheduling
                            .observe(height_diff as f64);

                        let data_certificate_with_delegation_metadata =
                            DataCertificateWithDelegationMetadata {
                                data_certificate: cert,
                                certificate_delegation_metadata,
                            };

                        let response = internal.query(
                            query,
                            state,
                            Some(data_certificate_with_delegation_metadata),
                            enable_query_stats_tracking,
                            None,
                            None,
                        );

                        Ok((response, time))
                    }
                    None => Err(QueryExecutionError::CertifiedStateUnavailable),
                };

                let _ = tx.send(Ok(result));
            }
            start.elapsed()
        });
```

**File:** rs/execution_environment/src/query_handler/query_scheduler.rs (L66-71)
```rust
    pub fn push<F>(&self, canister_id: CanisterId, query: F)
    where
        F: FnOnce() -> Duration + Send + 'static,
    {
        self.scheduler.push(canister_id, Query(Box::new(query)));
    }
```
