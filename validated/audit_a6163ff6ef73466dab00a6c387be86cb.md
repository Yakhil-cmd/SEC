Audit Report

## Title
Missing Concurrency Limit on Synchronous Call Endpoints Allows Targeted Resource Exhaustion - (File: `rs/http_endpoints/public/src/lib.rs`)

## Summary

The `make_router` function in `rs/http_endpoints/public/src/lib.rs` applies `GlobalConcurrencyLimitLayer` to every API route except the three synchronous call endpoints (`call_v3_router`, `call_v4_router`, `subnet_call_v4_router`), which are merged bare. Each request to these endpoints holds an async task open for up to 10 seconds awaiting ingress certification, enabling an unprivileged attacker to exhaust replica resources — Tokio task budget, ingress-watcher subscription slots, and memory — and degrade availability for all users of the affected node.

## Finding Description

In `rs/http_endpoints/public/src/lib.rs` lines 598–609, every router receives a `service_builder(GlobalConcurrencyLimitLayer::new(...))` wrapper except the three synchronous call routers:

```rust
.merge(http_handler.call_v2_router.layer(service_builder(
    GlobalConcurrencyLimitLayer::new(config.max_call_concurrent_requests), // 50
)))
// TODO(CON-1574): see if there is any reasonable explicit concurrency limit we could use here.
.merge(http_handler.call_v3_router)
.merge(http_handler.call_v4_router)
.merge(http_handler.subnet_call_v4_router)
```

The `new_router` function in `rs/http_endpoints/public/src/call/call_sync.rs` (lines 143–168) constructs these routers with only a `DefaultBodyLimit::disable()` layer — no concurrency cap.

Each request dispatched to `call_sync` (lines 192–327 of `call_sync.rs`) follows this resource-consuming path:
1. Calls `validate_ingress_message`, which spawns a blocking task for cryptographic verification.
2. Calls `ingress_watcher_handle.subscribe_for_certification(message_id)` — allocating a subscription slot in the ingress watcher's unbounded `HashMap<MessageId, ...>`.
3. Submits the message to the ingress pool.
4. Blocks on `certification_subscriber.wait_for_certification().timeout(Duration::from_secs(ingress_message_certificate_timeout_seconds))` — holding the task open for up to 10 seconds (default from `rs/config/src/http_handler.rs` line 88).

The only global backstop is the outer `.load_shed()` at `lib.rs` line 681, which only sheds load when the entire Tokio runtime is saturated — far too late to prevent per-endpoint exhaustion. The ingress watcher channel (`INGRESS_WATCHER_CHANNEL_SIZE = 1000` in `ingress_watcher.rs` line 22) is 20× larger than the v2 cap of 50 and does not bound concurrent in-flight requests.

## Impact Explanation

This matches the **High ($2,000–$10,000)** bounty impact: *Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS.*

N concurrent requests to `/api/v3/canister/{id}/call` cause N concurrent blocking tasks for signature verification, N live ingress-watcher subscription slots held for up to 10 seconds each, and N ingress pool entries. When the ingress pool fills, all callers (including v2 call, query, and read_state) receive `503`. This is a targeted resource-exhaustion attack exploiting a specific missing limit, not raw volumetric DDoS — the 10-second hold time amplifies impact so that even ~100–200 concurrent requests can saturate the endpoint.

## Likelihood Explanation

The endpoints are publicly reachable with no authentication requirement beyond a syntactically valid CBOR `HttpRequestEnvelope` (anonymous sender is accepted). The missing limit is explicitly acknowledged in a `TODO(CON-1574)` comment in the production source. HTTP/2 `max_concurrent_streams` defaults to 1000 per connection (`http_handler.rs` line 78), meaning a single TCP connection can issue enough streams to exhaust the endpoint. The attack is repeatable and requires no special privileges or victim interaction.

## Recommendation

Apply the same `GlobalConcurrencyLimitLayer` pattern to `call_v3_router`, `call_v4_router`, and `subnet_call_v4_router` as is already done for `call_v2_router`. Introduce a dedicated config field (e.g., `max_sync_call_concurrent_requests`) in `rs/config/src/http_handler.rs` with a conservative default (e.g., 50–100):

```rust
.merge(http_handler.call_v3_router.layer(service_builder(
    GlobalConcurrencyLimitLayer::new(config.max_sync_call_concurrent_requests),
)))
.merge(http_handler.call_v4_router.layer(service_builder(
    GlobalConcurrencyLimitLayer::new(config.max_sync_call_concurrent_requests),
)))
.merge(http_handler.subnet_call_v4_router.layer(service_builder(
    GlobalConcurrencyLimitLayer::new(config.max_sync_call_concurrent_requests),
)))
```

## Proof of Concept

Send N concurrent POST requests with a minimal valid CBOR `HttpRequestEnvelope` (anonymous sender) to `/api/v3/canister/aaaaa-aa/call` on a local replica or PocketIC instance. Each request will pass validation, acquire an ingress-watcher subscription, and block for up to 10 seconds. Observe via the `replica_http_ingress_watcher_tracked_messages` Prometheus gauge that tracked messages grow unboundedly, and that concurrent requests to `/api/v2/canister/{id}/call` or `/api/v2/canister/{id}/query` begin receiving `503 Service Unavailable` (load-shed) responses once the Tokio runtime saturates. A deterministic integration test can assert that after N > `max_call_concurrent_requests` concurrent v3 calls are in-flight, a v2 call is rejected, while the same test with the `GlobalConcurrencyLimitLayer` applied to v3 routers causes the excess v3 requests to be shed instead.