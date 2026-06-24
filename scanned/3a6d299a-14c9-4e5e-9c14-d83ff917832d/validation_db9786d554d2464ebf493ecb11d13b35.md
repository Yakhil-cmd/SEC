The vulnerability is real. Let me confirm the exact code paths before rendering a verdict.

**1. `RequestContext::hash` and `RequestContext::eq` — `request_type` is absent from both:** [1](#0-0) 

The comment on line 91 explicitly states these are "implemented for request caching," yet `request_type` is never hashed or compared. The key is only `(canister_id, sender, method_name, ingress_expiry, arg/http_request)`.

**2. `RequestType::is_query()` returns `true` for both `QueryV2` and `QueryV3`:** [2](#0-1) 

**3. `BypasserIC::bypass` only bypasses non-query types — both `QueryV2` and `QueryV3` pass through to the cache:** [3](#0-2) 

**4. The cache key extractor uses `Arc<RequestContext>` directly:** [4](#0-3) 

---

### Title
Cross-Version Cache Key Collision Between QueryV2 and QueryV3 — (`rs/boundary_node/ic_boundary/src/routes.rs`, `http/middleware/cache.rs`)

### Summary
`RequestContext::hash` and `RequestContext::eq` omit `request_type` from the cache key. Because `RequestType::is_query()` returns `true` for both `QueryV2` and `QueryV3`, both request types pass the `BypasserIC` check and enter the same cache namespace. Two requests differing only in `request_type` (V2 vs V3) are treated as identical cache entries.

### Finding Description
In `routes.rs` lines 94–125, `Hash` and `Eq` for `RequestContext` cover `canister_id`, `sender`, `method_name`, `ingress_expiry`, and `arg` — but not `request_type`. In `http/mod.rs` line 63, `is_query()` matches `QueryV2 | QueryV3 | QuerySubnetV3`. In `cache.rs` line 74, the bypasser only skips non-query types. Therefore:

- A `QueryV3` request with fields `(C, anonymous, M, E, A)` is forwarded to a replica, and the **certified** V3 CBOR response (containing a node signature) is stored under key `(C, anonymous, M, E, A)`.
- A subsequent `QueryV2` request with identical fields hits the cache and receives the **QueryV3 certified response** — a response format the V2 client did not request and may not validate correctly.
- The reverse is equally possible: a `QueryV2` response (no certificate) is cached and served to a `QueryV3` client that expects a certified response, silently stripping the certification guarantee.

### Impact Explanation
The more dangerous direction is **QueryV2 populates cache → QueryV3 client receives uncertified response**. A QueryV3 client is expected to verify the node signature/certificate in the response. Receiving a cached QueryV2 response (which has no certificate field) means the client either errors out or — if it treats a missing certificate as acceptable — accepts an uncertified result as if it were certified. This breaks the certified-response integrity guarantee that QueryV3 was designed to provide.

### Likelihood Explanation
The attacker is fully unprivileged. The only precondition is that the boundary node has query caching enabled (the default path when `cache_size` is configured). The attacker controls the HTTP endpoint and request body. No keys, admin access, or network-level attack is required. The collision is deterministic and reproducible.

### Recommendation
Add `request_type` to both `Hash` and `Eq` for `RequestContext`:

```rust
// In Hash impl:
self.request_type.hash(state);

// In PartialEq impl:
&& self.request_type == other.request_type
```

Alternatively, split the cache into per-version namespaces, or add a version discriminant to the key extractor in `KeyExtractorContext::extract`.

### Proof of Concept
```rust
// Unit test: assert QueryV2 and QueryV3 contexts with identical fields collide
let ctx_v2 = RequestContext {
    request_type: RequestType::QueryV2,
    canister_id: Some(principal),
    sender: Some(ANONYMOUS_PRINCIPAL),
    method_name: Some("foo".into()),
    ingress_expiry: Some(42),
    arg: Some(vec![1,2,3]),
    ..Default::default()
};
let ctx_v3 = RequestContext { request_type: RequestType::QueryV3, ..ctx_v2.clone() };

// Both of these currently pass — demonstrating the bug:
assert_eq!(ctx_v2, ctx_v3);  // PartialEq ignores request_type
assert_eq!(hash_of(&ctx_v2), hash_of(&ctx_v3));  // Hash ignores request_type
```

### Citations

**File:** rs/boundary_node/ic_boundary/src/routes.rs (L91-125)
```rust
/// Hash and Eq are implemented for request caching
/// They should both work on the same fields so that
/// k1 == k2 && hash(k1) == hash(k2)
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
