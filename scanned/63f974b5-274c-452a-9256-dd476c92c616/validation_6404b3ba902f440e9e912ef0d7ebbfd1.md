The root cause is confirmed. Let me verify the exact lines and the MAX_INGRESS_TTL constant to complete the analysis.

### Title
Boundary Node Query Cache Pollution via `ingress_expiry` Cache Key Inclusion — (`rs/boundary_node/ic_boundary/src/routes.rs`)

---

### Summary

The boundary node's query response cache uses `ingress_expiry` as part of its cache key. Because `ingress_expiry` is semantically irrelevant to the query result, an unprivileged attacker can flood the cache with entries that differ only in `ingress_expiry`, evicting all legitimate cached responses and forcing every other user to experience cache misses.

---

### Finding Description

The cache key for the boundary node query cache is `Arc<RequestContext>`. The `Hash` and `PartialEq` implementations for `RequestContext` are hand-written and explicitly include `ingress_expiry`: [1](#0-0) [2](#0-1) 

`ingress_expiry` is a nanosecond-precision UNIX timestamp indicating when the request envelope expires. Within the valid `MAX_INGRESS_TTL` window (approximately 5 minutes), there are on the order of 3×10¹¹ distinct valid nanosecond values. The query result is entirely independent of this field — two requests with identical `(canister_id, sender, method_name, arg)` but different `ingress_expiry` values will produce the same replica response, yet they are treated as distinct cache entries.

The `BypasserIC::bypass` function only bypasses caching for non-query request types, requests carrying a `nonce`, and non-anonymous senders (when `cache_non_anonymous` is false): [3](#0-2) 

There is no bypass or normalization for varying `ingress_expiry`. The `KeyExtractorContext` simply clones the full `Arc<RequestContext>` as the key: [4](#0-3) 

The cache is built with a finite `cache_size`: [5](#0-4) 

---

### Impact Explanation

An attacker sends N anonymous QueryV2 requests to a single canister endpoint (e.g., governance `get_proposal_info`, ledger `account_balance`) with identical `(canister_id, sender, method_name, arg)` but monotonically incrementing `ingress_expiry` values, all within the valid TTL window. Each request inserts a new, distinct LRU cache entry. Once N exceeds `cache_size / entry_size`, the LRU eviction policy begins expelling legitimate cached responses for all other users. From that point forward, every legitimate user query to that endpoint is a cache miss, forcing a full replica round-trip. This amplifies query load on subnet nodes proportionally to the number of users affected.

---

### Likelihood Explanation

The attack requires no privileges, no keys, and no special tooling — only the ability to send HTTP POST requests to the boundary node's public API. The attacker controls `ingress_expiry` directly in the CBOR-encoded request body. The valid window provides an enormous keyspace. Rate limiting per IP exists at the boundary node, but the attacker can distribute requests across multiple IPs or simply stay within per-IP limits while still saturating a small cache. The attack is fully local-testable as described in the proof idea.

---

### Recommendation

Remove `ingress_expiry` from both `Hash` and `PartialEq` for `RequestContext`. The query result is independent of `ingress_expiry`; it should not be part of the cache key. The corrected implementations should hash and compare only `(canister_id, sender, method_name, arg)` (and `http_request` for HTTP-type queries). [6](#0-5) 

---

### Proof of Concept

```
for i in 0..cache_size/entry_size + 1:
    send POST /api/v2/canister/<TARGET>/query with:
        canister_id = TARGET
        sender      = anonymous
        method_name = "account_balance"
        arg         = <fixed_arg>
        ingress_expiry = now_ns + i   # distinct per request, all within MAX_INGRESS_TTL

# Now send the baseline request (ingress_expiry = now_ns + 0)
response = send POST /api/v2/canister/<TARGET>/query with ingress_expiry = now_ns + 0
assert response.cache_status == MISS   # legitimate entry was evicted
```

Each iteration inserts a new LRU entry keyed on the unique `ingress_expiry`. After `cache_size/entry_size` iterations the original entry is evicted. The cache hit rate for any fixed baseline request drops to zero.

### Citations

**File:** rs/boundary_node/ic_boundary/src/routes.rs (L91-126)
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
impl Eq for RequestContext {}
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

**File:** rs/boundary_node/ic_boundary/src/http/middleware/cache.rs (L99-106)
```rust
        let cache = CacheBuilder::new_with_bypasser(KeyExtractorContext, bypasser)
            .cache_size(cli.cache_size.unwrap())
            .max_item_size(cli.cache_max_item_size)
            .ttl(cli.cache_ttl)
            .registry(registry)
            .methods(&[Method::POST])
            .build()
            .context("unable to build Cache")?;
```
