Audit Report

## Title
Cache Key Collision Between QueryV2 and QueryV3 Enables Cross-Version Response Poisoning — (`rs/boundary_node/ic_boundary/src/routes.rs`)

## Summary
`RequestContext::Hash` and `RequestContext::PartialEq` omit `request_type` from their implementations, causing `QueryV2` and `QueryV3` requests with identical `(canister_id, sender, method_name, ingress_expiry, arg)` tuples to map to the same cache key. Because both request types pass the `BypasserIC` check and share the same `CacheState` instance, the boundary node will serve a response formatted for one API version to a client of the other, producing a structurally mismatched CBOR certificate that conformant clients reject.

## Finding Description
`RequestContext` declares `request_type` as a field (line 69 of `routes.rs`) but the manually implemented `Hash` (lines 94–108) feeds only `canister_id`, `sender`, `method_name`, `ingress_expiry`, and `arg` into the hasher. `PartialEq` (lines 111–125) compares the same five fields. `request_type` is absent from both.

`KeyExtractorContext::extract` returns `Arc<RequestContext>` directly as the cache key (lines 36–45 of `cache.rs`), so the cache's equality and hash checks use these deficient implementations.

`BypasserIC::bypass` only bypasses when `!ctx.request_type.is_query()` (line 74 of `cache.rs`), and `is_query()` returns `true` for both `QueryV2` and `QueryV3` (line 63 of `http/mod.rs`), so both request types are admitted to the cache.

Both `PATH_QUERY_V2` and `PATH_QUERY_V3` are registered in `canister_query_routes` (lines 856–858 of `core.rs`) and merged into `canister_routes`, which receives the single shared `canister_layers` containing `cache_middleware` backed by one `CacheState` instance (lines 1016–1049 of `core.rs`).

The replica produces structurally different CBOR responses for the two versions: V2 embeds NNS delegation canister ranges in flat format; V3 embeds them in tree format. This is a tested protocol invariant (lines 479–533 of `nns_delegation_test.rs`). The boundary node caches the raw response body, so a cached V2 body is served verbatim to a V3 client.

## Impact Explanation
A conformant V3 client (or SDK) that strictly validates the delegation format will reject a V2-formatted certificate, causing a client-visible request failure. The reverse also holds. The impact is **availability/correctness**: clients receive cryptographically signed but protocol-version-mismatched responses. There is no confidentiality or integrity violation — the node signature over the response content remains valid. This matches the Medium bounty impact: a forged or stale certified response accepted only under constrained conditions, or moderate user-security impact.

## Likelihood Explanation
An unprivileged attacker can self-trigger the collision deterministically: send a V2 POST to `/api/v2/canister/{id}/query` with a chosen tuple to populate the cache, then immediately send a V3 POST to `/api/v3/canister/{id}/query` with the identical tuple within the TTL window (default 1 s). No special privileges or network position are required. Targeting a specific victim requires predicting their `ingress_expiry`, which is a meaningful constraint; however, for anonymous queries to public canisters with predictable method calls, the attacker can enumerate likely `ingress_expiry` values or race the cache population.

## Recommendation
Include `request_type` in both `Hash` and `PartialEq` for `RequestContext`, as the comment at lines 91–93 of `routes.rs` already states both impls must operate on the same fields:

```rust
// In Hash::hash()
self.request_type.hash(state);

// In PartialEq::eq()
&& self.request_type == other.request_type
```

## Proof of Concept
```rust
let ctx_v2 = RequestContext {
    request_type: RequestType::QueryV2,
    canister_id: Some(principal),
    sender: Some(ANONYMOUS_PRINCIPAL),
    method_name: Some("foo".into()),
    ingress_expiry: Some(0),
    arg: Some(vec![1, 2, 3]),
    ..Default::default()
};
let ctx_v3 = RequestContext { request_type: RequestType::QueryV3, ..ctx_v2.clone() };

assert_eq!(ctx_v2, ctx_v3);           // passes today — bug confirmed
assert_eq!(hash_of(&ctx_v2), hash_of(&ctx_v3)); // passes today — bug confirmed

// 1. POST /api/v2/canister/{id}/query with above params → CacheStatus::Miss, body = V2_BODY
// 2. POST /api/v3/canister/{id}/query with same params  → CacheStatus::Hit,  body = V2_BODY ← wrong version
// V3 client rejects flat-format delegation, request fails
```