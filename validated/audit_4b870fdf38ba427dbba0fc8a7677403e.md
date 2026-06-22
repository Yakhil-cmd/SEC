### Title
`RequestContext` Cache Key Excludes `nonce` Field, Enabling Cache Collision - (File: `rs/boundary_node/ic_boundary/src/routes.rs`)

### Summary
The `Hash` and `PartialEq` implementations for `RequestContext` — used as the boundary node's request cache key — omit the `nonce` field. Two requests that are identical except for their `nonce` hash to the same value and compare as equal, causing the cache to serve a previously stored response regardless of the nonce the caller supplied.

### Finding Description
In `rs/boundary_node/ic_boundary/src/routes.rs`, `RequestContext` is explicitly documented as a cache key:

> "Hash and Eq are implemented for request caching / They should both work on the same fields so that k1 == k2 && hash(k1) == hash(k2)"

The struct carries a `nonce: Option<Vec<u8>>` field: [1](#0-0) 

However, neither the `Hash` implementation nor the `PartialEq` implementation includes `nonce`: [2](#0-1) 

The hashed/compared fields are only `canister_id`, `sender`, `method_name`, `ingress_expiry`, and either `http_request` or `arg`. The `nonce` field is silently dropped from the cache identity.

The IC interface spec defines `nonce` as the mechanism to make otherwise-identical requests distinct. The `representation_independent_hash_call_or_query` function used for ingress message IDs correctly includes `nonce` in the hash: [3](#0-2) 

The boundary node cache key does not follow the same completeness.

### Impact Explanation
Any user who sends a query request with a `nonce` value different from a previously cached request — but with the same `canister_id`, `sender`, `method_name`, `ingress_expiry`, and `arg` — will receive the cached response from the earlier request. The nonce's purpose (forcing a fresh execution or distinguishing logically separate invocations) is silently defeated at the boundary node layer. For anonymous queries (where `sender` is the shared anonymous principal), any boundary node user can collide with any other anonymous user's cached entry by matching the remaining fields, receiving a stale or attacker-pre-seeded response.

### Likelihood Explanation
The attack path requires only the ability to send HTTP query requests to the boundary node — no credentials, no privileged role. For anonymous queries the `sender` field is identical for all callers, making collisions trivially constructible. The `ingress_expiry` field is included in the key, so the collision window is bounded by the expiry TTL, but within that window the attack is straightforward.

### Recommendation
Add `nonce` to both the `Hash` and `PartialEq` implementations of `RequestContext` so that requests differing only in their nonce are treated as distinct cache entries, consistent with how `representation_independent_hash_call_or_query` handles the nonce for ingress message identity.

### Proof of Concept
1. Send an anonymous query to canister `C`, method `M`, arg `A`, `ingress_expiry = T`, `nonce = [0x01]`. The boundary node caches the response under key `(C, anon, M, T, A)`.
2. Send the identical anonymous query with `nonce = [0x02]` (intending a fresh execution).
3. The `Hash` and `PartialEq` implementations produce the same key `(C, anon, M, T, A)` — `nonce` is not compared — so the boundary node returns the cached response from step 1 without forwarding the request to the replica.
4. The caller receives a stale (or attacker-pre-seeded) response despite having supplied a distinct nonce.

### Citations

**File:** rs/boundary_node/ic_boundary/src/routes.rs (L66-82)
```rust
/// Per-request information
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

**File:** rs/boundary_node/ic_boundary/src/routes.rs (L94-125)
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
```

**File:** rs/types/types/src/messages/http.rs (L43-79)
```rust
pub(crate) fn representation_independent_hash_call_or_query(
    request_type: CallOrQuery,
    canister_id: &[u8],
    method_name: &str,
    arg: &[u8],
    ingress_expiry: u64,
    sender: &[u8],
    nonce: Option<&[u8]>,
    sender_info: Option<RawSignedSenderInfoSlices<'_>>,
) -> [u8; 32] {
    use RawHttpRequestVal::*;
    let mut map = btreemap! {
        "request_type" => match request_type {
            CallOrQuery::Call => String("call"),
            CallOrQuery::Query => String("query"),
        },
        "canister_id" => Bytes(canister_id),
        "method_name" => String(method_name),
        "arg" => Bytes(arg),
        "ingress_expiry" => U64(ingress_expiry),
        "sender" => Bytes(sender),
    };
    if let Some(some_nonce) = nonce {
        map.insert("nonce", Bytes(some_nonce));
    }
    if let Some(RawSignedSenderInfoSlices { info, signer, sig }) = sender_info {
        map.insert(
            "sender_info",
            Map(btreemap! {
                "info" => Bytes(info),
                "signer" => Bytes(signer),
                "sig" => Bytes(sig),
            }),
        );
    }
    hash_of_map(&map, |key, value| hash_key_val(key, value))
}
```
