The code evidence is complete. Here is the full analysis.

All cited code is verified against the actual repository:

- `RequestContext` struct has `request_type` field at [1](#0-0) 
- `Hash` impl omits `request_type` at [2](#0-1) 
- `PartialEq` impl omits `request_type` at [3](#0-2) 
- `BypasserIC::bypass` passes QueryV2, QueryV3, QuerySubnetV3 through to cache at [4](#0-3) 
- `is_query()` returns true for all three at [5](#0-4) 
- `KeyExtractorContext::extract` uses `Arc<RequestContext>` as cache key at [6](#0-5) 

The collision is real and the exploit path is valid.

---

Audit Report

## Title
QueryV2/QueryV3 Cache Key Collision Serves Uncertified Response to Certified-Response Callers — (`rs/boundary_node/ic_boundary/src/http/middleware/cache.rs`, `routes.rs`)

## Summary
`RequestContext::Hash` and `RequestContext::PartialEq` both omit `request_type` from their implementations. Because `BypasserIC` passes `QueryV2`, `QueryV3`, and `QuerySubnetV3` through the cache (all satisfy `is_query()`), a `QueryV2` response cached under a given `(canister_id, sender, method_name, ingress_expiry, arg)` tuple will be served as a cache hit to a subsequent `QueryV3` request with identical parameters. `QueryV3` callers expect a node-signed certified response envelope; they receive an uncertified `QueryV2` body instead.

## Finding Description
`RequestContext` is defined with a `request_type` field (`routes.rs` line 69). The manual `Hash` implementation (`routes.rs` lines 94–108) hashes only `canister_id`, `sender`, `method_name`, `ingress_expiry`, and `arg`/`http_request` — `request_type` is absent. The manual `PartialEq` implementation (`routes.rs` lines 111–125) is consistent with `Hash` per the comment at lines 91–93, but both consistently omit `request_type`. `KeyExtractorContext::extract` (`cache.rs` lines 36–45) uses `Arc<RequestContext>` directly as the cache key, so the hash/eq behavior governs cache lookup. `BypasserIC::bypass` (`cache.rs` line 74) only bypasses requests where `!ctx.request_type.is_query()`; `is_query()` (`http/mod.rs` lines 62–64) returns `true` for `QueryV2`, `QueryV3`, and `QuerySubnetV3`, so all three enter the cache. Two `RequestContext` values differing only in `request_type` (`QueryV2` vs `QueryV3`) produce identical hashes and compare equal, causing the cache to treat them as the same entry. A `QueryV2` response stored first will be returned on a subsequent `QueryV3` lookup with matching parameters.

## Impact Explanation
`QueryV3` (`/api/v3/canister/{id}/query`) is the IC API endpoint that provides a node-signed certified response envelope. A `QueryV2` response carries no such signature. A client that relies on the boundary node to enforce the `QueryV3` certification contract — rather than independently verifying the node signature — will silently accept an uncertified response. This maps to the Medium allowed impact: **forged or stale certified response accepted only under constrained conditions**.

## Likelihood Explanation
The collision requires both requests to share the same `(canister_id, sender, method_name, ingress_expiry, arg)` tuple. The cache only stores anonymous queries by default (`cache_non_anonymous` guard, `cache.rs` line 80), so `sender` is fixed to the anonymous principal. The binding constraint is `ingress_expiry`. Two realistic paths: (1) **Self-collision** — a single client sends `QueryV2` then `QueryV3` with the same `ingress_expiry` (e.g., a client library upgrading from v2 to v3 mid-session, or retrying with the same expiry); this is fully attacker-controlled. (2) **Cross-client** — an attacker pre-populates the cache with a `QueryV2` response using a predictable or observed `ingress_expiry`, then waits for a victim to issue `QueryV3` with the same value. Anonymous queries use client-chosen timestamps, making prediction feasible when clients use rounded or fixed expiry windows. No privileged access, key material, or network interception is required.

## Recommendation
Include `request_type` in both `Hash` and `PartialEq` for `RequestContext`. The comment at `routes.rs` lines 91–93 explicitly states the contract ("They should both work on the same fields"), so adding `request_type` to both implementations is the minimal, consistent fix:

```rust
impl Hash for RequestContext {
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.request_type.hash(state);  // add this
        self.canister_id.hash(state);
        // ...
    }
}

impl PartialEq for RequestContext {
    fn eq(&self, other: &Self) -> bool {
        self.request_type == other.request_type  // add this
        && self.canister_id == other.canister_id
        // ...
    }
}
```

## Proof of Concept
```rust
// Unit test demonstrating hash/eq collision
let principal = Principal::from_text("aaaaa-aa").unwrap();
let ctx_v2 = RequestContext {
    request_type: RequestType::QueryV2,
    canister_id: Some(principal),
    sender: Some(ANONYMOUS_PRINCIPAL),
    method_name: Some("foo".into()),
    ingress_expiry: Some(12345),
    arg: Some(vec![1, 2, 3]),
    ..Default::default()
};
let ctx_v3 = RequestContext { request_type: RequestType::QueryV3, ..ctx_v2.clone() };

assert_eq!(hash(&ctx_v2), hash(&ctx_v3));  // passes — collision confirmed
assert_eq!(ctx_v2, ctx_v3);               // passes — collision confirmed

// Integration test:
// 1. POST /api/v2/canister/{id}/query with fixed params → assert CacheStatus::Miss, response stored
// 2. POST /api/v3/canister/{id}/query with identical params → assert CacheStatus::Hit
// 3. Assert response body is identical to step 1 and contains no node-signature field
```

### Citations

**File:** rs/boundary_node/ic_boundary/src/routes.rs (L68-69)
```rust
pub struct RequestContext {
    pub request_type: RequestType,
```

**File:** rs/boundary_node/ic_boundary/src/routes.rs (L94-108)
```rust
impl Hash for RequestContext {
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.canister_id.hash(state);
        self.sender.hash(state);
        self.method_name.hash(state);
        self.ingress_expiry.hash(state);

        // Hash http_request if it's present, arg otherwise
        // They're mutually exclusive
        if self.http_request.is_some() {
            self.http_request.hash(state);
        } else {
            self.arg.hash(state);
        }
    }
```

**File:** rs/boundary_node/ic_boundary/src/routes.rs (L111-125)
```rust
impl PartialEq for RequestContext {
    fn eq(&self, other: &Self) -> bool {
        let r = self.canister_id == other.canister_id
            && self.sender == other.sender
            && self.method_name == other.method_name
            && self.ingress_expiry == other.ingress_expiry;

        // Same as in hash()
        if self.http_request.is_some() {
            r && self.http_request == other.http_request
        } else {
            r && self.arg == other.arg
        }
    }
}
```

**File:** rs/boundary_node/ic_boundary/src/http/middleware/cache.rs (L36-45)
```rust
    fn extract<T>(&self, req: &Request<T>) -> Result<Self::Key, CacheError> {
        let ctx = req
            .extensions()
            .get::<Arc<RequestContext>>()
            .ok_or_else(|| {
                CacheError::ExtractKey("unable to get RequestContext extension".into())
            })?;

        Ok(ctx.clone())
    }
```

**File:** rs/boundary_node/ic_boundary/src/http/middleware/cache.rs (L74-76)
```rust
        Ok(if !ctx.request_type.is_query() {
            // We cache only Query
            Some(BypassReasonIC::IncorrectRequestType)
```

**File:** rs/boundary_node/ic_boundary/src/http/mod.rs (L62-64)
```rust
    pub const fn is_query(&self) -> bool {
        matches!(self, Self::QueryV2 | Self::QueryV3 | Self::QuerySubnetV3)
    }
```
