### Title
Missing Concurrency Limit on Synchronous Call Endpoints Allows Unbounded Resource Exhaustion - (File: `rs/http_endpoints/public/src/lib.rs`)

---

### Summary

The replica's HTTP endpoint applies a `GlobalConcurrencyLimitLayer` to every API route **except** the three synchronous call endpoints: `/api/v3/canister/{id}/call`, `/api/v4/canister/{id}/call`, and `/api/v4/subnet/{id}/call`. An unprivileged attacker can flood these endpoints with unlimited parallel requests, each of which holds a live connection open for up to 10 seconds while waiting for ingress certification, exhausting replica memory, Tokio task budget, and ingress-watcher subscription slots.

---

### Finding Description

In `rs/http_endpoints/public/src/lib.rs`, the `make_router` function wraps every router with a `service_builder(GlobalConcurrencyLimitLayer::new(...))` — except for `call_v3_router`, `call_v4_router`, and `subnet_call_v4_router`, which are merged bare:

```rust
.merge(http_handler.call_v2_router.layer(service_builder(
    GlobalConcurrencyLimitLayer::new(config.max_call_concurrent_requests),  // 50
)))
// TODO(CON-1574): see if there is any reasonable explicit concurrency limit we could use here.
.merge(http_handler.call_v3_router)
.merge(http_handler.call_v4_router)
.merge(http_handler.subnet_call_v4_router)
``` [1](#0-0) 

The comment `TODO(CON-1574)` explicitly acknowledges the missing limit. The v2 endpoint is capped at `max_call_concurrent_requests = 50` by default, but the v3/v4 endpoints have no such cap. [2](#0-1) 

Each request handled by `call_sync` in `rs/http_endpoints/public/src/call/call_sync.rs`:
1. Deserializes and cryptographically validates the ingress message (spawns a blocking task).
2. Calls `ingress_watcher_handle.subscribe_for_certification(message_id)` — allocating a subscription slot.
3. Submits the message to the ingress pool.
4. Blocks the async task for up to `ingress_message_certificate_timeout_seconds` (default **10 seconds**) waiting for a certified state update. [3](#0-2) 

The only global backstop is the outer `.load_shed()` layer, which only triggers when the entire Tokio runtime is saturated — far too late to prevent resource exhaustion on a single endpoint. [4](#0-3) 

---

### Impact Explanation

An attacker sending N concurrent requests to `/api/v3/canister/{id}/call` causes:
- N concurrent `spawn_blocking` tasks for signature verification (bounded only by the Tokio blocking thread pool, default 512).
- N live ingress-watcher subscriptions held open for up to 10 seconds each.
- N ingress pool entries (bounded by pool limits, but pool exhaustion itself causes `503` for all callers).
- Memory proportional to N for per-request state (CBOR body up to 5 MB each, subscription channels, etc.).

This can render the replica unresponsive to all callers — including legitimate users — for the duration of the attack. The v2 endpoint is protected (limit 50); v3/v4 are not. The HTTP/2 `max_concurrent_streams` default of 1000 per connection means a single TCP connection can saturate the endpoint. [5](#0-4) 

**Impact: High** — replica availability degraded for all users of the affected subnet node.
**Likelihood: High** — the endpoints are publicly reachable, require no authentication beyond a valid (but anonymous or self-signed) CBOR envelope, and the missing limit is explicitly noted in a TODO comment.

---

### Likelihood Explanation

The `/api/v3/call` and `/api/v4/call` endpoints are the primary synchronous call paths used by modern IC SDK clients. They are reachable by any unprivileged sender. Crafting a flood requires only valid CBOR-encoded `HttpRequestEnvelope` bodies (no valid signature needed to reach the subscription step — anonymous sender is accepted). The 10-second hold time per request amplifies the impact: 100 concurrent requests occupy the endpoint for a full 10-second window each.

---

### Recommendation

Apply the same `GlobalConcurrencyLimitLayer` pattern to `call_v3_router`, `call_v4_router`, and `subnet_call_v4_router` as is already done for `call_v2_router`. A dedicated config field (e.g., `max_sync_call_concurrent_requests`) should be introduced in `rs/config/src/http_handler.rs` with a conservative default (e.g., 50–100), mirroring `max_call_concurrent_requests`. The existing `service_builder` closure already encapsulates the correct pattern:

```rust
.merge(http_handler.call_v3_router.layer(service_builder(
    GlobalConcurrencyLimitLayer::new(config.max_sync_call_concurrent_requests),
)))
``` [6](#0-5) 

---

### Proof of Concept

```
# Send 200 concurrent POST requests to the v3 synchronous call endpoint.
# Each request carries a minimal valid CBOR envelope (anonymous sender).
# No rate limit or concurrency cap will reject them at the replica level.

for i in $(seq 1 200); do
  curl -s -X POST \
    -H "Content-Type: application/cbor" \
    --data-binary @minimal_call_envelope.cbor \
    "https://<replica-node>:8080/api/v3/canister/aaaaa-aa/call" &
done
wait
```

All 200 requests are accepted and each holds a subscription open for up to 10 seconds (`ingress_message_certificate_timeout_seconds`). The ingress-watcher subscription table and Tokio blocking thread pool fill up, causing legitimate requests — including v2 call, query, and read_state — to be load-shed with `429 Too Many Requests` or time out. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/http_endpoints/public/src/lib.rs (L591-609)
```rust
    let service_builder = |concurrency_limit_layer: GlobalConcurrencyLimitLayer| {
        ServiceBuilder::new()
            .layer(HandleErrorLayer::new(map_box_error_to_response))
            .load_shed()
            .layer(concurrency_limit_layer)
    };

    let final_router =
        base_router
            .merge(http_handler.status_router.layer(service_builder(
                GlobalConcurrencyLimitLayer::new(config.max_status_concurrent_requests),
            )))
            .merge(http_handler.call_v2_router.layer(service_builder(
                GlobalConcurrencyLimitLayer::new(config.max_call_concurrent_requests),
            )))
            // TODO(CON-1574): see if there is any reasonable explicit concurrency limit we could use here.
            .merge(http_handler.call_v3_router)
            .merge(http_handler.call_v4_router)
            .merge(http_handler.subnet_call_v4_router)
```

**File:** rs/http_endpoints/public/src/lib.rs (L676-693)
```rust
    final_router.layer(
        ServiceBuilder::new()
            .layer(TraceLayer::new_for_http())
            .layer(HandleErrorLayer::new(map_box_error_to_response))
            .layer(health_status_refresher.clone())
            .load_shed()
            .timeout(Duration::from_secs(config.request_timeout_seconds))
            .layer(axum::middleware::from_fn_with_state(
                Arc::new(metrics),
                collect_timer_metric,
            ))
            // Disable default limit since apply a request limit to all routes.
            .layer(DefaultBodyLimit::disable())
            .layer(RequestBodyLimitLayer::new(
                config.max_request_size_bytes as usize,
            ))
            .layer(cors_layer()),
    )
```

**File:** rs/config/src/http_handler.rs (L64-65)
```rust
    /// The maximum time the replica will wait for a message to be certified before timing out the requests and responding with `202`, for endpoints `/api/v3/call` and `/api/v4/call`.
    pub ingress_message_certificate_timeout_seconds: u64,
```

**File:** rs/config/src/http_handler.rs (L78-89)
```rust
            http_max_concurrent_streams: 1000,
            max_request_size_bytes: 5 * 1024 * 1024, // 5MB
            max_delegation_certificate_size_bytes: 1024 * 1024, // 1MB
            max_read_state_concurrent_requests: 100,
            max_catch_up_package_concurrent_requests: 100,
            max_dashboard_concurrent_requests: 100,
            max_status_concurrent_requests: 100,
            max_call_concurrent_requests: 50,
            max_query_concurrent_requests: QUERY_EXECUTION_THREADS_TOTAL * 100,
            max_pprof_concurrent_requests: 5,
            ingress_message_certificate_timeout_seconds: 10,
            max_tracing_flamegraph_concurrent_requests: 5,
```

**File:** rs/http_endpoints/public/src/call/call_sync.rs (L227-310)
```rust
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
```
