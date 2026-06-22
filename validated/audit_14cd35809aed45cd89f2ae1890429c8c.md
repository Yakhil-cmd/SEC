The code confirms this is a real, traceable vulnerability. Here is the full analysis:

---

### Title
Shared `QueryScheduler` Allows Unprivileged Query Flood to Delay Ingress Filter Decisions for a Target Canister — (`rs/execution_environment/src/query_handler/query_scheduler/internal.rs`, `rs/execution_environment/src/ingress_filter.rs`, `rs/execution_environment/src/lib.rs`)

---

### Summary

Both `IngressFilterServiceImpl` and `HttpQueryHandler` share the **same** `QueryScheduler` instance, which enforces a per-canister `max_threads_per_canister` concurrency limit. An unprivileged attacker who floods the scheduler with user queries for canister C can hold `active_threads` at the limit, causing any subsequently pushed ingress filter closure for canister C to be queued but not scheduled until the attacker's queries complete. This makes ingress filter latency for canister C directly controllable by query traffic.

---

### Finding Description

**1. Shared scheduler — confirmed in `setup_execution_helper`:**

A single `QueryScheduler` is constructed and then cloned into both services: [1](#0-0) 

The `Clone` on `QueryScheduler` clones the inner `Arc`, so both services share the same underlying `QuerySchedulerCore` state.

**2. Per-canister thread blocking — confirmed in `internal.rs`:**

`CanisterData::should_be_scheduled` returns `false` when `active_threads >= max_threads_per_canister`: [2](#0-1) 

In `push()`, a newly enqueued closure is added to `incoming` but is **not** added to the `scheduled` round-robin queue if the canister is already at its thread limit: [3](#0-2) 

The closure only becomes schedulable after `notify_finished_execution` decrements `active_threads`: [4](#0-3) 

**3. Ingress filter uses the same `push` path — confirmed in `ingress_filter.rs`:**

`IngressFilterServiceImpl::call` pushes the ingress filter closure using `canister_id` derived from the ingress message, into the shared scheduler: [5](#0-4) 

There is no priority lane, separate queue, or bypass for ingress filter closures. They are treated identically to user query closures by the scheduler.

**4. `HttpQueryHandler` uses the same scheduler:** [6](#0-5) 

---

### Impact Explanation

An attacker submits a continuous stream of user queries targeting canister C. With `max_threads_per_canister = N` (default is `query_execution_threads_per_canister`, typically 2), the attacker keeps `active_threads` for C at `N`. Any ingress filter closure for canister C pushed during this window is queued in `incoming` but blocked from scheduling. The legitimate caller's HTTP `/call` request awaits the `oneshot` channel response, which is only sent after the closure executes. This causes:

- Measurable, attacker-controlled latency on ingress filter decisions for canister C on the targeted replica node.
- HTTP-level timeouts for legitimate update callers if the delay exceeds client timeout thresholds.
- In extreme cases (long-running queries + short client timeouts), the caller may give up and not resubmit, effectively dropping the update message.

The ingress message expiry window (5 minutes) limits the risk of actual message loss from delay alone, but HTTP client timeouts (typically 30s) are a realistic failure mode.

---

### Likelihood Explanation

- Queries require no authentication beyond a valid canister ID and CBOR-encoded request body — any unprivileged internet user can submit them.
- The attacker only needs to keep `N` (typically 2) threads busy for canister C, which is achievable by submitting queries that run close to `max_instructions_per_query_message`.
- The attack is per-node and per-canister, so it is targeted and low-noise.
- No special knowledge of the target system is required beyond the canister ID.

---

### Recommendation

1. **Separate queues by traffic class**: Maintain distinct scheduler queues (or priority lanes) for ingress filter closures vs. user query closures, so ingress filter work is never blocked by user query concurrency limits.
2. **Dedicated thread budget for ingress filter**: Reserve a fixed number of threads exclusively for ingress filter execution, independent of `max_threads_per_canister`.
3. **Per-caller rate limiting on query submissions**: Limit the rate at which a single caller can submit queries to a given canister to prevent thread saturation.

---

### Proof of Concept

```
1. Configure a replica with max_threads_per_canister = 1.
2. Deploy canister C with a query method that runs near max_instructions_per_query_message.
3. Attacker: continuously submit query calls to canister C's query method.
4. Legitimate user: concurrently submit an update call to canister C.
5. Measure: time from update submission to HTTP response from ingress filter.
6. Expected: ingress filter response is delayed proportionally to attacker query load on C.
7. Baseline: repeat with no attacker queries; ingress filter responds in <10ms.
```

The `query_scheduler_respects_max_threads_per_canister` test in the test suite already demonstrates the blocking behavior: [7](#0-6) 

This test confirms that after `max_threads_per_canister` batches are popped, `try_pop()` returns `None` — meaning any further push (including an ingress filter closure) will not be scheduled until `notify_finished_execution` is called.

### Citations

**File:** rs/execution_environment/src/lib.rs (L417-431)
```rust
    let query_scheduler = QueryScheduler::new(
        config.query_execution_threads_total,
        config.embedders_config.query_execution_threads_per_canister,
        config.query_scheduling_time_slice_per_canister,
        metrics_registry,
    );

    let ingress_filter_metrics: Arc<_> = IngressFilterMetrics::new(metrics_registry).into();

    let ingress_filter = IngressFilterServiceImpl::new_service(
        query_scheduler.clone(),
        Arc::clone(&state_reader),
        Arc::clone(&exec_env),
        ingress_filter_metrics.clone(),
    );
```

**File:** rs/execution_environment/src/query_handler/query_scheduler/internal.rs (L83-93)
```rust
    // Returns true if the canister is blocked due to the max thread capacity
    // and cannot execute new queries until the pending executions finish.
    fn is_waiting_for_pending_executions(&self, max_threads_per_canister: usize) -> bool {
        self.active_threads >= max_threads_per_canister
    }

    // Returns true if the canister should be added to the `scheduled` canister
    // queue of the scheduler.
    fn should_be_scheduled(&self, max_threads_per_canister: usize) -> bool {
        self.has_queries() && !self.is_waiting_for_pending_executions(max_threads_per_canister)
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

**File:** rs/execution_environment/src/query_handler/query_scheduler/internal.rs (L219-237)
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
```

**File:** rs/execution_environment/src/ingress_filter.rs (L56-77)
```rust
        let canister_id = raw_ingress.content().canister_id();
        self.query_scheduler.push(canister_id, move || {
            let start = std::time::Instant::now();
            if !tx.is_closed() {
                let result = match state_reader.get_latest_certified_state() {
                    Some(state) => {
                        let v = exec_env.should_accept_ingress_message(
                            state.take(),
                            &provisional_whitelist,
                            &raw_ingress,
                            ExecutionMode::NonReplicated,
                            &metrics,
                        );
                        Ok(v)
                    }
                    None => Err(IngressFilterError::CertifiedStateUnavailable),
                };

                let _ = tx.send(Ok(result));
            }
            start.elapsed()
        });
```

**File:** rs/execution_environment/src/query_handler.rs (L528-584)
```rust
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

**File:** rs/execution_environment/src/query_handler/query_scheduler/tests.rs (L77-107)
```rust
#[test]
fn query_scheduler_respects_max_threads_per_canister() {
    let metrics_registry = MetricsRegistry::new();
    let scheduler = QuerySchedulerInternal::new(2, Duration::from_millis(1), &metrics_registry);
    for _ in 0..100 {
        scheduler.push(
            canister_test_id(0),
            Query(Box::new(move || std::time::Duration::from_millis(1000))),
        );
    }
    let batch1 = scheduler.pop().unwrap();
    assert!(!batch1.1.is_empty());

    let batch2 = scheduler.pop().unwrap();
    assert!(!batch2.1.is_empty());

    let batch3 = scheduler.try_pop();
    assert!(batch3.is_none());

    scheduler.notify_finished_execution(
        canister_test_id(0),
        std::time::Duration::from_millis(1000),
        vec![],
    );

    let batch4 = scheduler.pop().unwrap();
    assert!(!batch4.1.is_empty());

    let batch5 = scheduler.try_pop();
    assert!(batch5.is_none());
}
```
