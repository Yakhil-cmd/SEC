The code is clear. Let me trace every relevant piece.

**`RequestContext` Hash/PartialEq** — `routes.rs` lines 94–125: [1](#0-0) 

`request_type` is **not hashed**. Only `canister_id`, `sender`, `method_name`, `ingress_expiry`, and `arg`/`http_request` are included.

**`PartialEq`** mirrors this exactly: [2](#0-1) 

**`is_query()` returns true for both `QueryV2` and `QueryV3`:** [3](#0-2) 

**The bypasser only skips non-query types, nonce, and non-anonymous — not version:** [4](#0-3) 

**Both routes are active and share the same handler:** [5](#0-4) 

**The cache key extractor returns `Arc<RequestContext>` directly:** [6](#0-5) 

---

### Title
Cache Key Missing `request_type` Causes QueryV2/QueryV3 Cross-Version Cache Collision — (`rs/boundary_node/ic_boundary/src/routes.rs`)

### Summary
The `RequestContext` `Hash` and `PartialEq` implementations used as the cache key omit the `request_type` field. Because `QueryV2` and `QueryV3` both pass the `is_query()` bypass check, an unprivileged attacker can cause the boundary node cache to serve a `QueryV2` response to a `QueryV3` client (or vice versa) by sending two requests with identical `canister_id`, `sender`, `method_name`, `ingress_expiry`, and `arg`.

### Finding Description
`RequestContext::hash()` and `RequestContext::eq()` deliberately exclude `request_type` (and `request_size`). The comment says they implement caching equality, but the omission means two structurally different request types — `QueryV2` and `QueryV3` — that share all content fields are treated as the same cache key.

`RequestType::is_query()` returns `true` for `QueryV2`, `QueryV3`, and `QuerySubnetV3`, so none of these are bypassed by `BypasserIC`. The `KeyExtractorContext` extracts the full `Arc<RequestContext>` as the key, and the cache uses its `Hash`/`Eq` for lookup.

Exploit sequence:
1. Send `POST /api/v2/canister/{id}/query` with content fields `{canister_id, sender, method_name, ingress_expiry, arg}` → cache **miss**, response stored.
2. Send `POST /api/v3/canister/{id}/query` with identical content fields → cache **hit**, returns the `QueryV2` response.

### Impact Explanation
`QueryV3` returns a **certified** response with an embedded certificate in the CBOR body; `QueryV2` returns an **uncertified** response. Serving a `QueryV2` response to a `QueryV3` client means:
- The client receives a response without a certificate when it expected one.
- Clients that enforce certificate verification will reject the response (availability impact / DoS).
- Clients that do not strictly enforce certificate presence will silently consume an uncertified response in a context where they expected certification — this is the certification-bypass scenario.

The reverse (QueryV3 cached, served to QueryV2 client) is lower severity: the extra certificate data is typically ignored.

### Likelihood Explanation
The attack requires no privileges. The attacker only needs to control the timing of two requests to the same boundary node with matching content fields. Both API paths are simultaneously active in production. The `ingress_expiry` field is attacker-controlled and can be set to any value, making it trivial to craft matching pairs. This is concretely exploitable and locally testable.

### Recommendation
Include `request_type` in both `Hash` and `PartialEq` for `RequestContext`:

```rust
impl Hash for RequestContext {
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.request_type.hash(state);  // ADD THIS
        self.canister_id.hash(state);
        self.sender.hash(state);
        self.method_name.hash(state);
        self.ingress_expiry.hash(state);
        if self.http_request.is_some() {
            self.http_request.hash(state);
        } else {
            self.arg.hash(state);
        }
    }
}
```

Apply the same fix to `PartialEq::eq()`.

### Proof of Concept
```rust
// Two RequestContexts differing only in request_type
let ctx_v2 = RequestContext {
    request_type: RequestType::QueryV2,
    canister_id: Some(principal),
    sender: Some(ANONYMOUS_PRINCIPAL),
    method_name: Some("foo".into()),
    ingress_expiry: Some(12345),
    arg: Some(vec![1, 2, 3]),
    ..Default::default()
};
let ctx_v3 = RequestContext {
    request_type: RequestType::QueryV3,
    ..ctx_v2.clone()
};

// These are equal and hash-equal under the current implementation
assert_eq!(ctx_v2, ctx_v3);  // PASSES — BUG

use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};
let mut h1 = DefaultHasher::new(); ctx_v2.hash(&mut h1);
let mut h2 = DefaultHasher::new(); ctx_v3.hash(&mut h2);
assert_eq!(h1.finish(), h2.finish());  // PASSES — BUG
```

A QueryV2 response cached first will be returned for a subsequent QueryV3 request with identical content fields, serving an uncertified response to a client that requested certified query semantics.

### Citations

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

**File:** rs/boundary_node/ic_boundary/src/http/mod.rs (L62-64)
```rust
    pub const fn is_query(&self) -> bool {
        matches!(self, Self::QueryV2 | Self::QueryV3 | Self::QuerySubnetV3)
    }
```

**File:** rs/boundary_node/ic_boundary/src/http/middleware/cache.rs (L33-46)
```rust
impl KeyExtractor for KeyExtractorContext {
    type Key = Arc<RequestContext>;

    fn extract<T>(&self, req: &Request<T>) -> Result<Self::Key, CacheError> {
        let ctx = req
            .extensions()
            .get::<Arc<RequestContext>>()
            .ok_or_else(|| {
                CacheError::ExtractKey("unable to get RequestContext extension".into())
            })?;

        Ok(ctx.clone())
    }
}
```

**File:** rs/boundary_node/ic_boundary/src/http/middleware/cache.rs (L74-86)
```rust
        Ok(if !ctx.request_type.is_query() {
            // We cache only Query
            Some(BypassReasonIC::IncorrectRequestType)
        } else if ctx.nonce.is_some() {
            // Bypass cache if there's a nonce
            Some(BypassReasonIC::Nonce)
        } else if ctx.is_anonymous() == Some(false) && !self.cache_non_anonymous {
            // Bypass non-anonymous requests if not configured to cache them
            Some(BypassReasonIC::NonAnonymous)
        } else {
            None
        })
    }
```

**File:** rs/boundary_node/ic_boundary/src/core.rs (L856-858)
```rust
    let canister_query_routes = Router::new()
        .route(PATH_QUERY_V2, canister_handler.clone())
        .route(PATH_QUERY_V3, canister_handler.clone());
```
