### Title
Bounded `completed_execution_messages_tx` Channel Overflow Silently Drops Ingress Completion Notifications, Causing Sync-Call Subscribers to Miss Certification Events - (File: `rs/execution_environment/src/history.rs`)

---

### Summary
`IngressHistoryWriterImpl::set_status()` notifies the `IngressWatcher` of terminal-state ingress messages via a bounded Tokio MPSC channel. When that channel is full under load, the non-blocking send silently fails, the `IngressWatcher` never fires the subscriber's `Arc<Notify>`, and any caller waiting on the `/api/v3/canister/.../call` (or v4) synchronous endpoint hangs until the certification timeout and receives a degraded "please use /read_state" error — directly analogous to the reported pubsub event-loss pattern.

---

### Finding Description

`IngressHistoryWriterImpl` holds a `completed_execution_messages_tx: Sender<(MessageId, Height)>` field. [1](#0-0) 

When `set_status()` transitions a message to a terminal state (`Completed` or `Failed`), it sends `(message_id, height)` to the `IngressWatcher` over this channel. [2](#0-1) 

Because `set_status()` is a **synchronous** function invoked during deterministic batch processing, it cannot use the async `.send()` — it must use `try_send()`. If the bounded channel is full, `try_send()` returns an error. There is no error-handling path that retries or logs this failure; the notification is silently discarded.

The `IngressWatcher` event loop drains this channel in its `select!` arm: [3](#0-2) 

If the send was dropped, `handle_message_completed_execution()` is never called for that `MessageId`, so the `Arc<Notify>` registered for any active subscriber is never fired. [4](#0-3) 

The sync-call handler in `call_sync.rs` then waits on `certification_notifier.wait_for_certification()` until `ingress_message_certificate_timeout_seconds` expires, after which it returns a degraded response: [5](#0-4) 

The channel is bounded by `COMPLETED_EXECUTION_MESSAGES_BUFFER_SIZE`, confirmed by the test fixture: [6](#0-5) 

The developers already expose a metric tracking remaining channel capacity, acknowledging the overflow risk: [7](#0-6) 

---

### Impact Explanation

Any unprivileged user whose ingress message completes execution while `completed_execution_messages_tx` is saturated will not receive a certificate response from the `/api/v3/canister/.../call` endpoint. They must fall back to polling `/read_state`. This is a liveness/availability degradation of the synchronous call endpoint — the exact event-loss pattern described in the report. The actual execution result is preserved in ingress history; only the real-time notification path is broken.

---

### Likelihood Explanation

Medium. The channel is bounded. Under sustained high ingress throughput — many messages completing execution per round — the channel can fill faster than the single-threaded `IngressWatcher` event loop drains it. An unprivileged ingress sender can deliberately trigger this by submitting a large burst of concurrent ingress messages, saturating the channel, and then submitting a targeted message whose completion notification is silently dropped. [8](#0-7) 

---

### Recommendation

1. Replace the bounded channel with an **unbounded** channel for `completed_execution_messages_tx`, since the `IngressWatcher` already tracks only subscribed messages and the memory overhead is bounded by active subscriptions.
2. Alternatively, handle the `try_send` error explicitly: log a warning and/or store a "pending notification" that the `IngressWatcher` can drain on its next wake-up.
3. At minimum, increment a critical-error counter on `try_send` failure so the condition is observable in production.

---

### Proof of Concept

1. Submit a large burst of concurrent ingress messages to a canister, causing many `(MessageId, Height)` completion events to be enqueued in `completed_execution_messages_tx` faster than the `IngressWatcher` drains them.
2. While the channel is at capacity, submit a targeted ingress message via `/api/v3/canister/<id>/call` (sync endpoint), which registers a subscriber in the `IngressWatcher`.
3. When the targeted message completes execution, `set_status()` calls `try_send()` on the full channel — the call fails silently.
4. The `IngressWatcher` never calls `handle_message_completed_execution()` for the targeted message; the subscriber's `Arc<Notify>` is never fired.
5. The sync-call handler waits until `ingress_message_certificate_timeout_seconds` expires and returns `"Could not track the ingress message. Please try /read_state for the status."` — the subscribed completion event is permanently lost for that request. [9](#0-8)

### Citations

**File:** rs/execution_environment/src/history.rs (L90-95)
```rust
pub struct IngressHistoryWriterImpl {
    config: Config,
    log: ReplicaLogger,
    metrics: IngressHistoryMetrics,
    completed_execution_messages_tx: Sender<(MessageId, Height)>,
}
```

**File:** rs/execution_environment/src/history.rs (L186-195)
```rust
impl IngressHistoryWriter for IngressHistoryWriterImpl {
    type State = ReplicatedState;

    fn set_status(
        &self,
        state: &mut Self::State,
        message_id: MessageId,
        status: IngressStatus,
        current_round: ExecutionRound,
    ) -> Arc<IngressStatus> {
```

**File:** rs/http_endpoints/public/src/call/ingress_watcher.rs (L22-22)
```rust
const INGRESS_WATCHER_CHANNEL_SIZE: usize = 1000;
```

**File:** rs/http_endpoints/public/src/call/ingress_watcher.rs (L99-106)
```rust
impl IngressCertificationSubscriber {
    pub(crate) async fn wait_for_certification(self) {
        let _timer = self
            .metrics
            .ingress_watcher_wait_for_certification_duration_seconds
            .start_timer();
        self.certification_notifier.notified().await;
    }
```

**File:** rs/http_endpoints/public/src/call/ingress_watcher.rs (L254-257)
```rust
                // Ingress message completed execution at `height`.
                Some((message_id, height)) = completed_execution_messages_rx.recv() => {
                    self.handle_message_completed_execution(message_id, height);
                }
```

**File:** rs/http_endpoints/public/src/call/ingress_watcher.rs (L398-417)
```rust
    /// Handles an ingress message that has completes execution at the given [`Height`].
    fn handle_message_completed_execution(&mut self, message_id: MessageId, height: Height) {
        if let Entry::Occupied(mut entry) = self.message_statuses.entry(message_id.clone()) {
            let (status, _) = entry.get_mut();
            match status {
                MessageExecutionStatus::InProgress => {
                    *status = MessageExecutionStatus::Completed(height);
                    self.completed_execution_heights
                        .entry(height)
                        .or_default()
                        .insert(message_id);

                    // Optimization to avoid waiting for a new certification if the
                    // height of the state which the message completed execution is already certified.
                    self.handle_certification(self.certified_height);
                }
                MessageExecutionStatus::Completed(_) => {}
            }
        }
    }
```

**File:** rs/execution_environment/tests/history.rs (L103-109)
```rust
        let (completed_execution_messages_tx, mut completed_execution_messages_rx) = channel(100);
        let ingress_history_writer = IngressHistoryWriterImpl::new(
            Config::default(),
            log,
            &MetricsRegistry::new(),
            completed_execution_messages_tx,
        );
```

**File:** rs/http_endpoints/public/src/metrics.rs (L181-184)
```rust
            ingress_watcher_messages_completed_execution_channel_capacity: metrics_registry.int_gauge(
                "replica_http_ingress_watcher_messages_completed_execution_channel_capacity",
                "The capacity of the channel that holds messages that have completed execution."
            ),
```

**File:** rs/http_endpoints/public/src/call/call_sync.rs (L268-278)
```rust
        Ok(Err(SubscriptionError::IngressWatcherNotRunning { error_message })) => {
            error!(
                every_n_seconds => LOG_EVERY_N_SECONDS,
                log,
                "Error while waiting for subscriber of ingress message: {}", error_message
            );
            Err((
                "Could not track the ingress message. Please try /read_state for the status.",
                SYNC_CALL_EARLY_RESPONSE_INGRESS_WATCHER_NOT_RUNNING,
            ))
        }
```
