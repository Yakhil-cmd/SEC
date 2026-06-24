All cited code has been verified against the actual repository. Here is the validation result:

---

Audit Report

## Title
Cross-Version Cache Collision: QueryV2 Response Served for QueryV3 Request — (`rs/boundary_node/ic_boundary/src/routes.rs`, `rs/boundary_node/ic_boundary/src/http/middleware/cache.rs`)

## Summary
The boundary node's `RequestContext` cache key omits `request_type` from both `Hash` and `PartialEq`. Because `is_query()` returns `true` for both `QueryV2` and `QueryV3`, a cached V2 response is unconditionally returned as a cache hit for a structurally identical V3 request. The V2 response carries a flat NNS delegation (`CanisterRangesFilter::Flat`) while V3 clients expect a tree-format delegation (`CanisterRangesFilter::Tree`), causing certificate validation failures for V3 clients.

## Finding Description
**Root cause:** `RequestContext::Hash` (lines 94–108, `routes.rs`) hashes only `canister_id`, `sender`, `method_name`, `ingress_expiry`, and `arg`/`http_request`. `RequestContext::PartialEq` (lines 111–125) compares the same fields. The `request_type` field — which distinguishes `QueryV2` from `QueryV3` — is never included in either implementation.

**Bypass check is insufficient:** `BypasserIC::bypass` in `cache.rs` (lines 74–76) only bypasses non-query requests via `!ctx.request_type.is_query()`. `is_query()` in `http/mod.rs` (line 63) returns `true` for `QueryV2`, `QueryV3`, and `QuerySubnetV3` alike, so neither version is bypassed.

**Key extractor uses `Arc<RequestContext>` directly:** `KeyExtractorContext::extract` in `cache.rs` (lines 33–46) clones the `Arc<RequestContext>` as the cache key. Since `Hash` and `PartialEq` on `RequestContext` ignore `request_type`, a V2 and V3 request with identical `(canister_id, sender, method_name, ingress_expiry, arg)` produce the same cache key.

**Different replica endpoints produce structurally different responses:** `Node::build_url` in `snapshot.rs` (lines 100–106) routes `QueryV2` to `/api/v2/canister/{id}/query` and `QueryV3` to `/api/v3/canister/{id}/query`. The replica's query handler in `query.rs` (lines 300–310) produces a flat NNS delegation for V2 (`CanisterRangesFilter::Flat`) and a Merkle-tree delegation for V3 (`CanisterRangesFilter::Tree(canister_id)`). These are structurally incompatible response bodies.

**Exploit flow:**
1. Attacker (or any client) sends `POST /api/v2/canister/{id}/query` with `(canister_id, sender, method_name, ingress_expiry, arg)` → cache miss, V2 flat-delegation response stored.
2. Any V3 client sends `POST /api/v3/canister/{id}/query` with identical fields → cache hit, V2 flat-delegation response returned.
3. V3 client attempts to validate the NNS delegation as a Merkle tree → validation fails.

**Existing guards are insufficient:** The test comment at `nns_delegation_test.rs` line 527 ("check that we don't return incorrect cached response") confirms developer awareness of the cross-version collision risk, but this guard exists only at the replica level. The boundary node cache layer has no such protection.

## Impact Explanation
V3 query clients routed through the boundary node cache receive V2-format responses with flat NNS delegations instead of the expected tree-format delegations. This causes certificate validation failures for any dApp or agent relying on V3 node-signature and delegation verification. This constitutes a significant boundary/API security impact with concrete user harm: V3 query responses become unverifiable, breaking the integrity guarantee of the V3 query API. This maps to **High ($2,000–$10,000)**: significant boundary/API security impact with concrete user or protocol harm.

## Likelihood Explanation
Both `/api/v2` and `/api/v3` query endpoints are active in production. Anonymous queries (the default for many dApps) are cached by default. The collision requires matching `(canister_id, sender, method_name, ingress_expiry, arg)` — an attacker controlling both requests can trivially set identical `ingress_expiry` values. Even without a deliberate attacker, two independent clients querying the same canister method with the same parameters through different API versions within the same cache TTL window will trigger the collision. No special privileges, keys, or network position are required.

## Recommendation
Include `request_type` in both `Hash` and `PartialEq` for `RequestContext` in `rs/boundary_node/ic_boundary/src/routes.rs`:

```rust
// In Hash::hash:
self.request_type.hash(state);

// In PartialEq::eq:
self.request_type == other.request_type && ...
```

This ensures V2 and V3 requests with otherwise identical parameters produce distinct cache keys and are never served each other's responses.

## Proof of Concept
```rust
// Unit test demonstrating the collision
let ctx_v2 = RequestContext {
    request_type: RequestType::QueryV2,
    canister_id: Some(principal),
    sender: Some(ANONYMOUS_PRINCIPAL),
    method_name: Some("foo".into()),
    ingress_expiry: Some(42),
    arg: Some(vec![1, 2, 3]),
    ..Default::default()
};
let ctx_v3 = RequestContext { request_type: RequestType::QueryV3, ..ctx_v2.clone() };

// These must differ but currently do not:
assert_eq!(ctx_v2, ctx_v3);               // passes — BUG
assert_eq!(hash(&ctx_v2), hash(&ctx_v3)); // passes — BUG
```

Integration test: extend the existing `interlaced_v2_and_v3_query_requests` test in `rs/tests/networking/nns_delegation_test.rs` (line 528) to route through the boundary node cache layer and assert that the V3 response contains a tree-format delegation, not a flat one. With the current code, the assertion will fail after the first V2 request populates the cache.