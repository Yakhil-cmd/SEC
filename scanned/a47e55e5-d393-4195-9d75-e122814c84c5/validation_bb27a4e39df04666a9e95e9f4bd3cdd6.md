The vulnerability is real and concrete. Here is the analysis:

---

### Title
Cross-Version Query Cache Collision: QueryV3 Requests Served Unsigned QueryV2 Responses — (`rs/boundary_node/ic_boundary/src/routes.rs`, `src/http/middleware/cache.rs`)

### Summary

The boundary node query cache uses `RequestContext` as its cache key. The custom `Hash` and `PartialEq` implementations for `RequestContext` deliberately omit the `request_type` field. Because `RequestType::is_query()` returns `true` for both `QueryV2` and `QueryV3`, both request types pass the cache bypass check and share the same cache namespace. An unprivileged client can populate the cache with a V2 response (no node BLS signature), then any subsequent QueryV3 request with identical parameters receives that V2 response — stripping the node signature that QueryV3 clients rely on for response authenticity.

### Finding Description

**Root cause — `request_type` excluded from cache key:**

`RequestContext` has a `request_type` field: [1](#0-0) 

But the custom `Hash` impl hashes only `canister_id`, `sender`, `method_name`, `ingress_expiry`, and `arg`/`http_request`: [2](#0-1) 

And `PartialEq` compares the same fields, also omitting `request_type`: [3](#0-2) 

**Both QueryV2 and QueryV3 pass the cache bypass check:**

`is_query()` returns `true` for both variants: [4](#0-3) 

The `BypasserIC` only bypasses non-query types, nonce-bearing requests, and (optionally) non-anonymous requests — it does **not** differentiate between V2 and V3: [5](#0-4) 

**Cache key extraction uses the defective `Hash`/`Eq`:** [6](#0-5) 

**The handler uses `request_type` to build the upstream URL** — but only on a cache miss. On a cache hit the upstream is never contacted: [7](#0-6) 

### Impact Explanation

QueryV3 (`/api/v3/canister/{id}/query`) is the IC API endpoint that returns a node BLS signature over the response, allowing clients to cryptographically verify which replica node produced the result. QueryV2 returns no such signature. When the cache serves a V2 response body for a V3 request:

- The client receives the correct query result data but **without a node signature**.
- Any client that uses QueryV3 specifically to verify response authenticity (e.g., light clients, agents with `verify_query_signatures = true`) either fails to verify (breaking functionality) or silently accepts an unverifiable response (breaking the security invariant).
- The boundary node effectively downgrades the security guarantee of QueryV3 to that of QueryV2 for any request whose parameters were previously cached via a V2 call.

### Likelihood Explanation

- Requires no privileges — any anonymous HTTP client can trigger it.
- The attacker simply sends a QueryV2 request first, then the victim's QueryV3 request hits the cache.
- The cache TTL keeps the collision active for the full configured duration.
- The attack is silent: the HTTP status code is 200 and the response body is valid CBOR; only the missing signature field reveals the downgrade.

### Recommendation

Include `request_type` in both `Hash` and `PartialEq` for `RequestContext`:

```rust
// In Hash impl (routes.rs ~line 95):
self.request_type.hash(state);

// In PartialEq impl (routes.rs ~line 113):
let r = self.request_type == other.request_type
    && self.canister_id == other.canister_id
    ...
``` [8](#0-7) 

### Proof of Concept

```rust
use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};

let mut ctx_v2 = RequestContext::default();
ctx_v2.request_type = RequestType::QueryV2;
ctx_v2.canister_id = Some(principal!("sqjm4-qahae-aq"));
ctx_v2.sender = Some(ANONYMOUS_PRINCIPAL);
ctx_v2.method_name = Some("foo".into());
ctx_v2.ingress_expiry = Some(12345);
ctx_v2.arg = Some(vec![1, 2, 3]);

let mut ctx_v3 = ctx_v2.clone();
ctx_v3.request_type = RequestType::QueryV3;

// These assert both pass — demonstrating the collision:
assert_eq!(ctx_v2, ctx_v3);  // PartialEq ignores request_type

let h = |ctx: &RequestContext| {
    let mut s = DefaultHasher::new();
    ctx.hash(&mut s);
    s.finish()
};
assert_eq!(h(&ctx_v2), h(&ctx_v3));  // Hash ignores request_type
```

A V2 response stored under this key is returned verbatim for the V3 request, delivering an unsigned response to a client that expected a BLS-signed one.

### Citations

**File:** rs/boundary_node/ic_boundary/src/routes.rs (L67-82)
```rust
#[derive(Debug, Clone, Default)]
pub struct RequestContext {
    pub request_type: RequestType,
    pub request_size: u32,

    // CBOR fields
    pub canister_id: Option<Principal>,
    pub sender: Option<Principal>,
    pub method_name: Option<String>,
    pub nonce: Option<Vec<u8>>,
    pub ingress_expiry: Option<u64>,
    pub arg: Option<Vec<u8>>,

    /// Filled in when the inner request is HTTP
    pub http_request: Option<HttpRequest>,
}
```

**File:** rs/boundary_node/ic_boundary/src/routes.rs (L94-126)
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
}

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
impl Eq for RequestContext {}
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

**File:** rs/boundary_node/ic_boundary/src/http/middleware/cache.rs (L66-87)
```rust
    fn bypass<T>(&self, req: &Request<T>) -> Result<Option<Self::BypassReason>, CacheError> {
        let ctx = req
            .extensions()
            .get::<Arc<RequestContext>>()
            .ok_or_else(|| {
                CacheError::ExecuteBypasser("unable to get RequestContext extension".into())
            })?;

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
}
```

**File:** rs/boundary_node/ic_boundary/src/http/handlers.rs (L244-251)
```rust
) -> Result<impl IntoResponse, ApiError> {
    let url = node
        .build_url(ctx.request_type, canister_id.into())
        .map_err(|e| ErrorCause::Other(format!("failed to build request url: {e}")))?;
    // Proxy the request
    let resp = p.proxy(request, url).await?;

    Ok(resp)
```
