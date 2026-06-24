Audit Report

## Title
Silent `try_send` Failure on Bounded `completed_execution_messages_tx` Channel Causes Sync-Call Subscribers to Hang Until Timeout - (File: `rs/execution_environment/src/history.rs`)

## Summary
`IngressHistoryWriterImpl::set_status()` uses `try_send()` with the result silently discarded (`let _ = ...`) to notify the `IngressWatcher` when an ingress message reaches a terminal state. When the bounded MPSC channel is full, the notification is permanently lost. Any active subscriber registered via the `/api/v3/canister/.../call` or `/api/v4/...` sync endpoint for that message will never have its `Arc<Notify>` fired, causing `wait_for_certification()` to block until `ingress_message_certificate_timeout_seconds` expires and return a degraded `202 Accepted` response instead of a certificate.

## Finding Description

**Root cause — silent discard of `try_send` error:**

In `rs/execution_environment/src/history.rs` at lines 307–322, when `set_status()` transitions a message to a terminal `IngressState` (`Completed` or `Failed`), it calls:

```rust
let _ = self.completed_execution_messages_tx.try_send((
    message_id.clone(),
    completed_execution_and_updated_to_terminal_state,
));
```

The `let _ = ...` pattern unconditionally discards the `Result`. If the bounded channel is full, `try_send` returns `Err(TrySendError::Full(...))` and the `(MessageId, Height)` pair is permanently dropped — no log, no retry, no counter increment.

**Why `try_send` must be used here:** `set_status()` is a synchronous function called during deterministic batch execution. It cannot `.await` an async `send()`, so `try_send()` is the only option. This is an architectural constraint that makes the silent-drop path unavoidable without a design change.

**IngressWatcher event loop — the notification never arrives:**

In `rs/http_endpoints/public/src/call/ingress_watcher.rs` at lines 254–257, the `IngressWatcher` drains the channel in a `select!` arm:

```rust
Some((message_id, height)) = completed_execution_messages_rx.recv() => {
    self.handle_message_completed_execution(message_id, height);
}
```

If the send was dropped, `handle_message_completed_execution` is never called for that `MessageId`. At lines 399–417, this function is the only place where a subscribed message's status is transitioned from `InProgress` to `Completed` and its `Arc<Notify>` is eventually fired via `handle_certification`. Without this call, the notifier is never triggered.

**Sync-call handler — timeout path:**

In `rs/http_endpoints/public/src/call/call_sync.rs` at lines 310–327, the handler wraps `wait_for_certification()` with a timeout:

```rust
match certification_subscriber
    .wait_for_certification()
    .timeout(Duration::from_secs(ingress_message_certificate_timeout_seconds))
    .await
{
    Ok(()) => (),
    Err(_) => {
        return SyncCallResponse::Accepted(
            "Message did not complete execution and certification within the replica defined timeout.",
        );
    }
}
```

When the notification is dropped, `wait_for_certification()` blocks indefinitely on `self.certification_notifier.notified().await` (line 105 of `ingress_watcher.rs`) until the timeout fires, returning `202 Accepted` instead of a certificate.

**Channel is bounded and the overflow is acknowledged:**

The test fixture at `rs/execution_environment/tests/history.rs` lines 103–109 creates the channel with `channel(100)`. The production replica setup also uses a bounded channel. The developers already expose a metric `replica_http_ingress_watcher_messages_completed_execution_channel_capacity` (metrics.rs lines 181–184) tracking remaining capacity, which is an implicit acknowledgment that overflow is a known risk.

**Existing guards are insufficient:**

`handle_message_completed_execution` at line 400 only processes messages already in `message_statuses` (i.e., with active subscriptions). The channel is filled by ALL terminal-state messages, including those without subscriptions. An attacker can flood the channel with completion events from non-subscribed messages, then the targeted subscribed message's notification is dropped.

## Impact Explanation

This is an application/platform-level availability degradation of the IC's synchronous call endpoints (`/api/v3/canister/.../call`, `/api/v4/...`). Any user relying on the sync endpoint to receive a certificate inline will instead receive a `202 Accepted` response and must fall back to polling `/read_state`. The execution result itself is preserved in ingress history; only the real-time notification path is broken. Under sustained exploitation, the sync call endpoint is effectively reduced to the behavior of the legacy async endpoint for all affected requests. This matches the allowed impact: **High ($2,000–$10,000) — Application/platform-level DoS or subnet availability impact not based on raw volumetric DDoS**, since the attack exploits a specific bounded-channel code vulnerability rather than raw network flooding.

## Likelihood Explanation

Medium. The channel is bounded. During high-throughput execution rounds — many messages completing per round — the channel can fill faster than the single-threaded `IngressWatcher` event loop drains it. An unprivileged user can deliberately trigger this by submitting a burst of concurrent ingress messages to any canister (causing many `(MessageId, Height)` completion events to be enqueued), then submitting a targeted message via the sync endpoint. When the targeted message completes while the channel is saturated, its notification is silently dropped. The attack requires no special privileges, no governance majority, and no node compromise. The IC's per-user ingress rate limits constrain the burst size, but the channel size (100 in tests, bounded in production) is small relative to the number of messages that can complete in a single execution round on a busy subnet.

## Recommendation

1. **Replace the bounded channel with an unbounded channel** for `completed_execution_messages_tx`. The `IngressWatcher` already filters out messages without active subscriptions in `handle_message_completed_execution`, so memory growth is bounded by the number of active sync-call subscribers, not by total ingress throughput.
2. **Alternatively**, on `try_send` failure, log a warning and/or store the `(MessageId, Height)` in a local pending buffer that the `IngressWatcher` can drain on its next scheduled wake-up.
3. **At minimum**, increment a critical-error counter on `try_send` failure so the condition is observable in production monitoring, rather than silently discarding the error.

## Proof of Concept

1. Spin up a local replica or use PocketIC with a test canister.
2. Submit a large burst of concurrent ingress messages (e.g., 200+ simple update calls) to fill `completed_execution_messages_tx` beyond its bounded capacity.
3. Concurrently, submit a targeted ingress message via `/api/v3/canister/<id>/call` (sync endpoint), which registers a subscriber in the `IngressWatcher`.
4. Observe that when the targeted message completes execution, `set_status()` calls `try_send()` on the full channel — the call returns `Err(Full(...))` and is discarded via `let _ = ...` at `history.rs:318`.
5. The `IngressWatcher` never calls `handle_message_completed_execution` for the targeted `MessageId`; the subscriber's `Arc<Notify>` is never fired.
6. The sync-call handler waits until `ingress_message_certificate_timeout_seconds` expires and returns `202 Accepted` with `"Message did not complete execution and certification within the replica defined timeout."` — the subscribed completion event is permanently lost for that request.
7. A deterministic integration test can reproduce this by: creating an `IngressHistoryWriterImpl` with `channel(1)`, calling `set_status()` twice for terminal-state messages (first fills the channel, second is dropped), then asserting that the `IngressWatcher` only processes one notification.