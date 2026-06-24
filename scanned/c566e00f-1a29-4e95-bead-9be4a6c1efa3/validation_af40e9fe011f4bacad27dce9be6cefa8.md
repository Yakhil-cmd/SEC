The code is clear and I have everything needed to evaluate this claim. Let me trace through the exact logic.

The code evidence is complete. Here is the full analysis:

---

### Title
QueryV2/QueryV3 Cache Key Collision Serves Uncertified Response to Certified-Response Callers — (`rs/boundary_node/ic_boundary/src/routes.rs`, `cache.rs`)

### Summary

`RequestContext::Hash` and `RequestContext::PartialEq` both omit `request_type` from their implementations. Because `BypasserIC` passes both `QueryV2` and `QueryV3` through the cache (both satisfy `is_query()`), a `QueryV2` response cached under a given `(canister_id, sender, method_name, ingress_expiry, arg)` tuple will be served as a cache hit to a subsequent `QueryV3` request with identical parameters. `QueryV3` callers expect a node-signed certified envelope; they receive an uncertified `QueryV2` body instead.

### Finding Description

**Step 1 — Both query variants enter the cache.**

`BypasserIC::bypass` only bypasses non-query types:

```rust
// cache.rs line 74
Ok(if !ctx.request_type.is_query() {
    Some(BypassReasonIC::IncorrectRequestType)
```

`is_query()` returns `true` for `QueryV2`, `QueryV3`, and `QuerySubnetV3`:

```rust
// http/mod.rs line 62-64
pub const fn is_query(&self) -> bool {
    matches!(self, Self::QueryV2 | Self::QueryV3 | Self::QuerySubnetV3)
}
``` [1](#0-0) [2](#0-1) 

**Step 2 — `request_type` is absent from the cache key.**

`RequestContext::Hash` hashes only `canister_id`, `sender`, `method_name`, `ingress_expiry`, and `arg`/`http_request`:

```rust
// routes.rs lines 95-108
impl Hash for RequestContext {
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.canister_id.hash(state);
        self.sender.hash(state);
        self.method_name.hash(state);
        self.ingress_expiry.hash(state);
        // request_type is NOT hashed
        ...
    }
}
```

`RequestContext::PartialEq` is consistent — it also omits `request_type`:

```rust
// routes.rs lines 112-124
fn eq(&self, other: &Self) -> bool {
    let r = self.canister_id == other.canister_id
        && self.sender == other.sender
        && self.method_name == other.method_name
        && self.ingress_expiry == other.ingress_expiry;
    // request_type is NOT compared
    ...
}
``` [3](#0-2) [4](#0-3) 

**Step 3 — `KeyExtractorContext::extract` uses `Arc<RequestContext>` directly as the cache key.**

```rust
// cache.rs lines 36-45
fn extract<T>(&self, req: &Request<T>) -> Result<Self::Key, CacheError> {
    let ctx = req.extensions().get::<Arc<RequestContext>>()...;
    Ok(ctx.clone())
}
``` [5](#0-4) 

**Collision proof:**

Two `RequestContext` values differing only in `request_type` (`QueryV2` vs `QueryV3`) produce identical hashes and compare equal. The cache therefore treats them as the same entry.

### Impact Explanation

`QueryV3` (`/api/v3/canister/{id}/query`) is the IC API endpoint that guarantees a node-signed certified response envelope. A `QueryV2` response carries no such signature. When the boundary node serves a cached `QueryV2` body to a `QueryV3` caller, the caller receives an uncertified response that the boundary node presents as a valid `QueryV3` reply. Any client that relies on the boundary node to enforce the `QueryV3` certification guarantee — rather than independently verifying the signature — will silently accept forged or stale data.

### Likelihood Explanation

For the collision to fire, both requests must share the same `(canister_id, sender, method_name, ingress_expiry, arg)` tuple. The `ingress_expiry` is the binding constraint. Two realistic paths:

1. **Self-collision**: A single client sends `QueryV2` then `QueryV3` with the same `ingress_expiry` (e.g., a client library that upgrades from v2 to v3 mid-session, or retries with the same expiry). This is fully attacker-controlled.
2. **Cross-client**: An attacker pre-populates the cache with a `QueryV2` response using a predictable or observed `ingress_expiry`, then waits for a victim to issue `QueryV3` with the same value. Anonymous queries (the only ones cached by default) use client-chosen timestamps, making prediction feasible when clients use rounded or fixed expiry windows.

The attack requires no privileged access, no key material, and no network-level interception. It is entirely within the boundary node's HTTP API surface.

### Recommendation

Include `request_type` in both `Hash` and `PartialEq` for `RequestContext`. The comment at line 91–93 of `routes.rs` explicitly states the contract ("They should both work on the same fields"), so adding `request_type` to both implementations is the minimal, consistent fix:

```rust
impl Hash for RequestContext {
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.request_type.hash(state);  // add this
        self.canister_id.hash(state);
        ...
    }
}

impl PartialEq for RequestContext {
    fn eq(&self, other: &Self) -> bool {
        self.request_type == other.request_type  // add this
        && self.canister_id == other.canister_id
        ...
    }
}
``` [6](#0-5) 

### Proof of Concept

```rust
// Demonstrates hash/eq collision
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

assert_eq!(hash(&ctx_v2), hash(&ctx_v3));  // passes
assert_eq!(ctx_v2, ctx_v3);               // passes

// Drive cache_middleware:
// 1. POST /api/v2/canister/{id}/query  → CacheStatus::Miss  (response stored)
// 2. POST /api/v3/canister/{id}/query  → CacheStatus::Hit   (QueryV2 body returned)
// Assert: response body is identical and contains no node signature field.
```

### Citations

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

**File:** rs/boundary_node/ic_boundary/src/routes.rs (L91-93)
```rust
/// Hash and Eq are implemented for request caching
/// They should both work on the same fields so that
/// k1 == k2 && hash(k1) == hash(k2)
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
