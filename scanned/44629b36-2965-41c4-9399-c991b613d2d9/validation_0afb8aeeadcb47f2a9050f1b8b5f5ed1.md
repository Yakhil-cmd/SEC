### Title
Missing Concurrency Limit on Synchronous Call Endpoints (`/api/v3`, `/api/v4`, `/api/v4/subnet`) Allows Shared Resource Exhaustion - (File: `rs/http_endpoints/public/src/lib.rs`)

### Summary
The IC replica's HTTP server applies a `GlobalConcurrencyLimitLayer` to the `/api/v2/canister/.../call` endpoint but explicitly omits it for the newer synchronous call endpoints (`/api/v3/canister/.../call`, `/api/v4/canister/.../call`, `/api/v4/subnet/.../call`). Each of these requests acquires a mutex on the shared `IngressFilterService`, a read-lock on the `IngressPoolThrottler`, and performs registry lookups. An unprivileged sender can flood these endpoints without bound, contending on shared locks and degrading node operation.

### Finding Description
In `make_router()` in `rs/http_endpoints/public/src/lib.rs`, every endpoint is wrapped with a `service_builder(GlobalConcurrencyLimitLayer::new(...))` except for `call_v3_router`, `call_v4_router`, and `subnet_call_v4_router`:

```rust
// call_v2 gets a concurrency limit
.merge(http_handler.call_v2_router.layer(service_builder(
    GlobalConcurrencyLimitLayer::new(config.max_call_concurrent_requests),
)))
// TODO(CON-1574): see if there is any reasonable explicit concurrency limit we could use here.
.merge(http_handler.call_v3_router)   // NO LIMIT
.merge(http_handler.call_v4_router)   // NO LIMIT
.merge(http_handler.subnet_call_v4_router)  // NO LIMIT
```

The TODO comment at line 606 explicitly acknowledges this gap. [1](#0-0) 

Each request processed by these routers calls `IngressValidator::validate_ingress_message()`, which:

1. Acquires a read-lock on the shared `ingress_throttler: Arc<RwLock<dyn IngressPoolThrottler>>` [2](#0-1) 
2. Acquires a blocking mutex lock on the shared `ingress_filter: Arc<Mutex<IngressFilterService>>` [3](#0-2) 
3. Performs registry client lookups (`get_ingress_message_settings`, `get_provisional_whitelist`) [4](#0-3) 
4. Spawns a blocking task for cryptographic signature verification [5](#0-4) 

The `ingress_filter` mutex is the same instance shared across all concurrent call processing on the node. [6](#0-5) 

### Impact Explanation
An unprivileged attacker sending a flood of well-formed (or even malformed) POST requests to `/api/v3/canister/{id}/call`, `/api/v4/canister/{id}/call`, or `/api/v4/subnet/{id}/call` can:

- Saturate the `ingress_filter` mutex, blocking legitimate call processing on the same node.
- Exhaust the Tokio blocking thread pool via unbounded `spawn_blocking` calls for signature verification.
- Cause registry client contention under high load.
- Degrade or stall the node's ability to accept legitimate ingress messages, reducing subnet throughput.

This is a node-level availability impact. The attacker does not need any special privileges — any boundary/API user can reach these endpoints.

### Likelihood Explanation
The `/api/v3` and `/api/v4` call endpoints are the primary modern call endpoints used by the `ic-agent` SDK and all current tooling. They are publicly reachable through boundary nodes. The attack requires only the ability to send HTTP POST requests, which any internet user can do. The absence of a concurrency limit is confirmed by the developer TODO comment, indicating this is a known gap rather than an intentional design choice.

### Recommendation
Apply `GlobalConcurrencyLimitLayer::new(config.max_call_concurrent_requests)` (with load-shedding) to `call_v3_router`, `call_v4_router`, and `subnet_call_v4_router` in `make_router()`, consistent with how `call_v2_router` is protected. This resolves the acknowledged TODO at line 606. [7](#0-6) 

### Proof of Concept
1. Identify a replica node's public HTTPS endpoint.
2. Send a large number of concurrent POST requests to `/api/v3/canister/aaaaa-aa/call` with any CBOR-encoded body (even a malformed one will pass the body-limit check and reach `validate_ingress_message`).
3. Observe that unlike `/api/v2/canister/.../call` (which returns `429 Too Many Requests` once `max_call_concurrent_requests` is exceeded), the v3/v4 endpoints accept all concurrent connections without bound.
4. Monitor the node: legitimate `/api/v2` and `/api/v3` call processing stalls as the `ingress_filter` mutex becomes contended and the blocking thread pool saturates.

The `call_v2_router` correctly sheds load at `config.max_call_concurrent_requests` concurrent requests. [8](#0-7)  The v3/v4 routers have no such protection. [9](#0-8)

### Citations

**File:** rs/http_endpoints/public/src/lib.rs (L290-303)
```rust
    let ingress_filter = Arc::new(Mutex::new(ingress_filter));

    let call_handler = IngressValidatorBuilder::builder(
        log.clone(),
        node_id,
        subnet_id,
        registry_client.clone(),
        ingress_verifier.clone(),
        ingress_filter.clone(),
        ingress_throttler.clone(),
        ingress_tx,
    )
    .with_malicious_flags(malicious_flags.clone())
    .build();
```

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

**File:** rs/http_endpoints/public/src/call.rs (L230-236)
```rust
        let ingress_pool_is_full = ingress_throttler.read().unwrap().exceeds_threshold();
        if ingress_pool_is_full {
            Err(HttpError {
                status: StatusCode::SERVICE_UNAVAILABLE,
                message: "Service is overloaded, try again later.".to_string(),
            })?;
        }
```

**File:** rs/http_endpoints/public/src/call.rs (L299-301)
```rust
        let registry_version = registry_client.get_latest_version();
        let (ingress_registry_settings, provisional_whitelist) =
            get_registry_data(&log, subnet_id, registry_version, registry_client.as_ref())?;
```

**File:** rs/http_endpoints/public/src/call.rs (L327-341)
```rust
        tokio::task::spawn_blocking(move || {
            validator.validate_request(
                &request_c,
                time_source.get_relative_time(),
                &root_of_trust_provider,
            )
        })
        .await
        .map_err(|_| HttpError {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            message: "".into(),
        })?
        .map_err(|validation_error| {
            validation_error_to_http_error(msg.as_ref(), validation_error, &log)
        })?;
```

**File:** rs/http_endpoints/public/src/call.rs (L343-357)
```rust
        let ingress_filter = ingress_filter.lock().unwrap().clone();

        match ingress_filter
            .oneshot((provisional_whitelist, msg.clone()))
            .await
            .expect("Can't panic on Infallible")
        {
            Err(IngressFilterError::CertifiedStateUnavailable) => {
                return Err(certified_state_unavailable_error().into());
            }
            Ok(Err(user_error)) => {
                Err(user_error)?;
            }
            Ok(Ok(())) => (),
        }
```
