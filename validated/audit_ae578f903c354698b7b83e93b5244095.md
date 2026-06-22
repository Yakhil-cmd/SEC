### Title
Unbounded Synchronous Connection Hold in `call_sync` Exhausts Replica Resources via Flooding — (File: `rs/http_endpoints/public/src/call/call_sync.rs`)

### Summary

The IC's synchronous call endpoint (`/api/v3/canister/.../call`, `/api/v4/canister/.../call`, `/api/v4/subnet/.../call`) implemented in `call_sync` holds each HTTP connection open while awaiting `wait_for_certification()` with no concurrency cap. Simultaneously, the `IngressWatcher` backing it maintains an **unbounded** `message_statuses` HashMap and `cancellations` JoinMap. An unprivileged external sender can flood the endpoint with valid ingress messages, exhausting HTTP connection resources and growing the `IngressWatcher`'s internal state without bound, degrading or denying service to the replica node.

### Finding Description

`call_sync` is the IC's synchronous ingress submission handler. After validating an ingress message and subscribing to the `IngressWatcher`, it **blocks the HTTP connection** by awaiting `wait_for_certification()` until either the message is certified or `ingress_message_certificate_timeout_seconds` elapses: [1](#0-0) 

There is no concurrency limit on the number of simultaneous `call_sync` requests that can reach this blocking await. Compare this to the asynchronous handler (`/api/v2/canister/.../call`), which explicitly bounds concurrent tracking tasks with a semaphore: [2](#0-1) 

The `IngressWatcher` that backs both handlers stores per-message state in an **unbounded** `HashMap` and `JoinMap`: [3](#0-2) 

Each unique message subscribed via `handle_ingress_message` inserts an entry into `message_statuses` and spawns a cancellation task in `cancellations` with no cap: [4](#0-3) 

The subscription channel is bounded at 1000 entries, but once a subscription is accepted and processed by the `IngressWatcher` event loop, the entry lives in the unbounded `message_statuses` HashMap until the message is certified or the subscriber drops: [5](#0-4) 

The `IngressWatcher` runs as a single-threaded `select!` event loop. As `message_statuses` and `cancellations` grow unboundedly, the loop's per-iteration work (iterating `cancellations.join_next()`, processing `completed_execution_heights`) increases, degrading throughput for all subscribers.

### Impact Explanation

An attacker flooding `/api/v3/canister/.../call` or `/api/v4/canister/.../call` with valid ingress messages (each with a unique message ID) causes:

1. **HTTP connection exhaustion**: Each concurrent request holds an open TCP connection for up to `ingress_message_certificate_timeout_seconds`. With no concurrency cap, this can exhaust file descriptors and connection-state memory on the replica.
2. **`IngressWatcher` memory exhaustion**: Each unique message adds an entry to the unbounded `message_statuses` HashMap and spawns a task in the unbounded `cancellations` JoinMap. Sustained flooding grows these structures without bound.
3. **`IngressWatcher` event loop starvation**: The single-threaded loop processes one event per iteration. A large `cancellations` JoinMap slows `join_next()` polling, delaying certification notifications for all legitimate subscribers.

The net effect is degraded or denied service to the replica's public API, impacting all users of the subnet.

### Likelihood Explanation

The `/api/v3/canister/.../call` and `/api/v4/canister/.../call` endpoints are publicly reachable by any unprivileged sender. Ingress message validation (signature, expiry, ingress pool throttling) provides some friction, but the ingress pool is sized to accommodate thousands of messages. A single attacker with a valid identity can submit thousands of distinct valid ingress messages (each with a unique nonce/expiry combination) in rapid succession, triggering the resource exhaustion. No privileged access, key compromise, or threshold corruption is required.

### Recommendation

1. **Add a concurrency limit to `call_sync`**: Mirror the async handler's `Semaphore` pattern. Reject or downgrade to `202 Accepted` when the limit is reached, analogous to `MAX_CONCURRENT_TRACKING_TASKS` in `call_async.rs`.
2. **Cap `IngressWatcher.message_statuses`**: Enforce a maximum size on the `message_statuses` HashMap. When the cap is reached, reject new subscriptions with `SubscriptionError` so `call_sync` falls back to `202 Accepted`.
3. **Reduce `ingress_message_certificate_timeout_seconds`**: A shorter timeout reduces the window during which connections are held open.

### Proof of Concept

```
# Attacker sends N concurrent valid ingress messages to the sync endpoint.
# Each message has a unique nonce, making each a distinct MessageId.
# Each request blocks the server connection for up to ingress_message_certificate_timeout_seconds.

for i in $(seq 1 5000); do
  curl -s -X POST https://<replica-node>/api/v3/canister/<canister-id>/call \
    -H "Content-Type: application/cbor" \
    --data-binary @<valid_ingress_cbor_with_unique_nonce_$i> &
done
wait

# Result:
# - 5000 open HTTP connections held for the full timeout duration
# - IngressWatcher.message_statuses grows to 5000 entries (unbounded)
# - IngressWatcher.cancellations grows to 5000 tasks (unbounded)
# - IngressWatcher event loop slows, delaying certification for legitimate users
# - File descriptor exhaustion possible on the replica node
```

The root cause is the absence of a concurrency guard in `call_sync` (analogous to the `ProposeAndWait` pattern in the external report) and the unbounded internal state of `IngressWatcher`. [6](#0-5) [3](#0-2) [2](#0-1)

### Citations

**File:** rs/http_endpoints/public/src/call/call_sync.rs (L192-327)
```rust
async fn call_sync(
    axum::extract::Path(id): axum::extract::Path<PrincipalId>,
    State(SynchronousCallHandlerState {
        call_handler,
        ingress_watcher_handle,
        metrics,
        ingress_message_certificate_timeout_seconds,
        state_reader,
        nns_delegation_reader,
        version,
    }): State<SynchronousCallHandlerState>,
    WithTimeout(Cbor(request)): WithTimeout<Cbor<HttpRequestEnvelope<HttpCallContent>>>,
) -> SyncCallResponse {
    let (effective_destination, delegation_filter) = match version {
        Version::V3 => {
            let canister_id = CanisterId::unchecked_from_principal(id);
            (
                EffectiveDestination::Canister(canister_id),
                CanisterRangesFilter::Flat,
            )
        }
        Version::V4 => {
            let canister_id = CanisterId::unchecked_from_principal(id);
            (
                EffectiveDestination::Canister(canister_id),
                CanisterRangesFilter::Tree(canister_id),
            )
        }
        Version::SubnetV4 => (
            EffectiveDestination::Subnet(SubnetId::from(id)),
            CanisterRangesFilter::None,
        ),
    };
    let log = call_handler.log.clone();

    let ingress_submitter = match call_handler
        .validate_ingress_message(request, effective_destination)
        .await
    {
        Ok(ingress_submitter) => ingress_submitter,
        Err(ingress_error) => return SyncCallResponse::from(ingress_error),
    };

    let message_id = ingress_submitter.message_id();

    // Check if the message is already known.
    // If it is known, we can return the certificate without re-submitting the message
    // to the ingress pool.
    if let Some((tree, certification)) =
        tree_and_certificate_for_message(state_reader.clone(), message_id.clone()).await
        && let ParsedMessageStatus::Known(_) = parsed_message_status(&tree, &message_id)
    {
        let signature = certification.signed.signature.signature.get().0;

        metrics
            .sync_call_early_response_trigger_total
            .with_label_values(&[SYNC_CALL_EARLY_RESPONSE_MESSAGE_ALREADY_IN_CERTIFIED_STATE])
            .inc();

        return SyncCallResponse::Certificate(Certificate {
            tree,
            signature: Blob(signature),
            delegation: nns_delegation_reader.get_delegation(delegation_filter),
        });
    };

    let certification_subscriber = match ingress_watcher_handle
        .subscribe_for_certification(message_id.clone())
        .timeout(SUBSCRIPTION_TIMEOUT)
        .await
    {
        Ok(Ok(message_subscriber)) => Ok(message_subscriber),
        Ok(Err(SubscriptionError::DuplicateSubscriptionError)) => Err((
            "Duplicate request. Message is already being tracked and executed.",
            SYNC_CALL_EARLY_RESPONSE_DUPLICATE_SUBSCRIPTION,
        )),
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
        Err(_) => {
            warn!(
                every_n_seconds => LOG_EVERY_N_SECONDS,
                log,
                "Timed out while submitting a certification subscription.";
            );
            Err((
                "Could not track the ingress message. Please try /read_state for the status.",
                SYNC_CALL_EARLY_RESPONSE_SUBSCRIPTION_TIMEOUT,
            ))
        }
    };

    let ingres_submission = ingress_submitter.try_submit();

    if let Err(ingress_submission) = ingres_submission {
        return SyncCallResponse::HttpError(ingress_submission);
    }
    // The ingress message was submitted successfully.
    // From this point on we only return a certificate or `Accepted 202``.
    let certification_subscriber = match certification_subscriber {
        Ok(certification_subscriber) => certification_subscriber,
        Err((reason, metric_label)) => {
            metrics
                .sync_call_early_response_trigger_total
                .with_label_values(&[metric_label])
                .inc();
            return SyncCallResponse::Accepted(reason);
        }
    };

    match certification_subscriber
        .wait_for_certification()
        .timeout(Duration::from_secs(
            ingress_message_certificate_timeout_seconds,
        ))
        .await
    {
        Ok(()) => (),
        Err(_) => {
            metrics
                .sync_call_early_response_trigger_total
                .with_label_values(&[SYNC_CALL_EARLY_RESPONSE_CERTIFICATION_TIMEOUT])
                .inc();
            return SyncCallResponse::Accepted(
                "Message did not complete execution and certification within the replica defined timeout.",
            );
        }
    }
```

**File:** rs/http_endpoints/public/src/call/call_async.rs (L30-52)
```rust
/// Used to bound the number of tokio tasks spawned for tracking the
/// certification time of messages. 10_000 is chosen as it is roughly
/// the pool size.
const MAX_CONCURRENT_TRACKING_TASKS: usize = 10_000;

#[derive(Clone)]
pub struct AsynchronousCallHandlerState {
    ingress_watcher_handle: Option<IngressWatcherHandle>,
    ingress_validator: IngressValidator,
    ingress_tracking_semaphore: Arc<Semaphore>,
}

impl AsynchronousCallHandlerState {
    pub fn new(
        ingress_validator: IngressValidator,
        ingress_watcher_handle: Option<IngressWatcherHandle>,
    ) -> Self {
        Self {
            ingress_validator,
            ingress_watcher_handle,
            ingress_tracking_semaphore: Arc::new(Semaphore::new(MAX_CONCURRENT_TRACKING_TASKS)),
        }
    }
```

**File:** rs/http_endpoints/public/src/call/ingress_watcher.rs (L22-22)
```rust
const INGRESS_WATCHER_CHANNEL_SIZE: usize = 1000;
```

**File:** rs/http_endpoints/public/src/call/ingress_watcher.rs (L119-133)
```rust
pub struct IngressWatcher {
    log: ReplicaLogger,
    metrics: HttpHandlerMetrics,
    rt_handle: Handle,
    cancellation_token: CancellationToken,
    /// Keeps track of the certified height.
    certified_height: Height,

    /// Maps message id to a future that resolves when all subscribers stop waiting for its certification.
    cancellations: JoinMap<MessageId, ()>,
    /// Maps the message id to its [`MessageExecutionStatus`] and a [`Notify`]er to notify its subscribers when the message is certified.
    message_statuses: HashMap<MessageId, (MessageExecutionStatus, Arc<Notify>)>,
    /// Inverse index, maps the height to the set of message ids that completed execution at that height.
    completed_execution_heights: BTreeMap<Height, HashSet<MessageId>>,
}
```

**File:** rs/http_endpoints/public/src/call/ingress_watcher.rs (L287-312)
```rust
        let certification_notifier = match self.message_statuses.entry(message.clone()) {
            // New message, create a new notifier.
            Entry::Vacant(vacant_entry) => {
                self.cancellations.spawn_on(
                    message.clone(),
                    cancellation_token.cancelled_owned(),
                    &self.rt_handle,
                );

                let certification_notifier = Arc::new(tokio::sync::Notify::new());
                vacant_entry.insert((
                    MessageExecutionStatus::InProgress,
                    certification_notifier.clone(),
                ));

                Ok(certification_notifier)
            }
            // Seen message, return the existing notifier. This can happen if the replica gets two or more requests for the same message.
            Entry::Occupied(_) => {
                self.metrics.ingress_watcher_duplicate_requests_total.inc();
                Err(SubscriptionError::DuplicateSubscriptionError)
            }
        };

        let _ = certification_notifier_tx.send(certification_notifier);
    }
```
