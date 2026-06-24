The code evidence is clear. Let me compile the full analysis.

The critical detail is now confirmed. For anonymous queries, `validate_ingress_expiry` is **explicitly skipped** at the replica level too (`rs/validator/src/ingress_validation.rs` line 140-142: `if !request.sender().get().is_anonymous() { validate_ingress_expiry(...)?; }`). This means an attacker can use **any** `u64` value for `ingress_expiry` in anonymous queries — not just values within the 5-minute TTL window — and both the boundary node cache and the replica will accept them.

---

### Title
Cache Key Pollution via Unbounded `ingress_expiry` in Anonymous Query Caching — (`rs/boundary_node/ic_boundary/src/routes.rs`)

### Summary

The `RequestContext` cache key includes `ingress_expiry` in both its `Hash` and `PartialEq` implementations. For anonymous query requests, neither the boundary node nor the replica validates `ingress_expiry` at all. An unprivileged attacker can send a flood of anonymous queries with identical `(canister_id, method_name, arg)` but incrementing arbitrary `ingress_expiry` values, causing every request to be treated as a distinct cache entry, exhausting the cache budget, evicting legitimate entries, and driving the cache hit rate to 0%.

### Finding Description

**Root cause 1 — `ingress_expiry` in cache key:**

`RequestContext::Hash` and `RequestContext::PartialEq` both include `ingress_expiry`: [1](#0-0) [2](#0-1) 

The `KeyExtractorContext` uses the full `Arc<RequestContext>` as the cache key: [3](#0-2) 

**Root cause 2 — No `ingress_expiry` validation for anonymous queries at the boundary node:**

`BypasserIC::bypass` only checks request type, nonce presence, and anonymity. There is no `ingress_expiry` guard before caching: [4](#0-3) 

**Root cause 3 — No `ingress_expiry` validation for anonymous queries at the replica either:**

The replica's `HttpRequestVerifier<Query>` implementation explicitly skips `validate_ingress_expiry` when the sender is anonymous: [5](#0-4) 

This is intentional by design (anonymous queries are stateless and don't need expiry enforcement), but it means the attacker's arbitrary `ingress_expiry` values are accepted end-to-end. The test `should_not_error_when_system_query_expired` confirms this behavior explicitly.

**Root cause 4 — `ingress_expiry` is semantically irrelevant to query results:**

For query calls, `ingress_expiry` does not affect the canister execution result. Two queries with identical `(canister_id, sender, method_name, arg)` but different `ingress_expiry` values are semantically identical and should share a cache entry. Including it in the key is a design error.

### Impact Explanation

An attacker sends anonymous queries with `ingress_expiry = 0, 1, 2, 3, …` (or any arbitrary `u64` sequence). Each is a distinct cache key. The cache (bounded by `cache_size`) fills with entries that will never be reused. Legitimate queries for the same canister/method/arg are evicted and must be forwarded to replicas on every request. The cache hit rate approaches 0%, and every query becomes a fresh upstream call, amplifying load on subnet replicas proportional to the attacker's request rate.

### Likelihood Explanation

The attack requires no credentials, no signature (anonymous sender), and no special knowledge. The attacker only needs to craft CBOR-encoded query envelopes with varying `ingress_expiry` fields — a trivial modification to any standard IC client library. The attack is self-amplifying: a modest request rate (e.g., 1,000 req/s) can continuously churn the cache if `cache_size` is in the tens of thousands of entries.

### Recommendation

Exclude `ingress_expiry` from `RequestContext::Hash` and `RequestContext::PartialEq`. For query caching purposes, two requests are semantically equivalent if they share `(canister_id, sender, method_name, arg/http_request)`. The `ingress_expiry` field is part of the request envelope for replay-protection of update calls and is irrelevant to query result identity. [6](#0-5) 

### Proof of Concept

```python
import cbor2, requests, time

CANISTER = "aaaaa-aa"
URL = f"https://boundary.ic0.app/api/v2/canister/{CANISTER}/query"

for i in range(10_000):
    envelope = {
        "content": {
            "request_type": "query",
            "canister_id": bytes.fromhex("00000000000000000101"),
            "method_name": "greet",
            "arg": b"\x44\x49\x44\x4c\x00\x01\x71\x05world",
            "sender": bytes([4]),          # anonymous principal
            "ingress_expiry": i,           # unique per request, arbitrary value
        }
    }
    requests.post(URL, data=cbor2.dumps(envelope),
                  headers={"Content-Type": "application/cbor"})

# Assert: cache-status header shows "MISS" on every response
# Assert: eviction counter on boundary node metrics == 9999
```

Each of the 10,000 requests produces a distinct cache key due to `ingress_expiry = i`. The cache fills with non-reusable entries, evicting all legitimate cached responses. Cache hit rate → 0%.

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

**File:** rs/boundary_node/ic_boundary/src/http/middleware/cache.rs (L63-87)
```rust
impl Bypasser for BypasserIC {
    type BypassReason = BypassReasonIC;

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

**File:** rs/validator/src/ingress_validation.rs (L134-151)
```rust
    fn validate_request(
        &self,
        request: &HttpRequest<Query>,
        current_time: Time,
        root_of_trust_provider: &R,
    ) -> Result<CanisterIdSet, RequestValidationError> {
        if !request.sender().get().is_anonymous() {
            validate_ingress_expiry(request, current_time)?;
        }
        let delegation_targets = validate_request_content(
            request,
            self.validator.as_ref(),
            current_time,
            root_of_trust_provider,
        )?;
        validate_request_target(request, &delegation_targets)?;
        Ok(delegation_targets)
    }
```
