Audit Report

## Title
Boundary Node Cache Key Collision Between `QueryV2`/`QueryV3` and `QuerySubnetV3` Due to Missing `request_type` in `RequestContext` Hash/Eq — (`rs/boundary_node/ic_boundary/src/routes.rs`)

## Summary
The `RequestContext` struct used as the boundary node cache key omits `request_type` from both its custom `Hash` and `PartialEq` implementations. Because `is_query()` returns `true` for `QueryV2`, `QueryV3`, and `QuerySubnetV3`, all three are eligible for caching. Two requests of different types but identical CBOR fields (`canister_id`, `sender`, `method_name`, `ingress_expiry`, `arg`) produce the same cache key, causing the boundary node to serve a cached response from one request type to a caller of a different type. A `QueryV3` or `QuerySubnetV3` client expecting a certified response envelope may receive a bare `QueryV2` response lacking a certificate.

## Finding Description
`RequestContext` is defined with a `request_type: RequestType` field at line 69 of `routes.rs`, but the manually implemented `Hash` (lines 94–108) and `PartialEq` (lines 111–125) cover only `canister_id`, `sender`, `method_name`, `ingress_expiry`, and `arg`/`http_request`. The `request_type` field is never hashed or compared.

`is_query()` in `http/mod.rs` line 63 explicitly includes `QuerySubnetV3` alongside `QueryV2` and `QueryV3`. The `BypasserIC::bypass` in `cache.rs` line 74 only bypasses caching when `!ctx.request_type.is_query()`, so all three query variants enter the cache. `KeyExtractorContext::extract` (lines 33–45 of `cache.rs`) returns the full `Arc<RequestContext>` as the cache key, relying entirely on the broken `Hash`/`Eq`.

Exploit flow:
1. Attacker sends a `QueryV2` POST to `/api/v2/canister/ic_00/query` with chosen `(sender, method_name, ingress_expiry, arg)` → `CacheStatus::Miss`, response stored.
2. A `QueryV3` or `QuerySubnetV3` request arrives with identical CBOR fields and the same `ingress_expiry` → `Hash` and `PartialEq` treat it as the same key → `CacheStatus::Hit`, cached `QueryV2` response body returned.
3. The `QueryV3`/`QuerySubnetV3` client receives a bare CBOR reply with no certificate instead of the expected certified response envelope.

Existing guards are insufficient: the nonce bypass (line 77) and non-anonymous bypass (line 80) do not distinguish between request types, and no other mechanism prevents cross-type cache hits.

## Impact Explanation
A `QueryV3` or `QuerySubnetV3` client receives a response body that lacks the certificate/envelope it expects. Clients that enforce certificate verification will error; clients operating in non-strict or lightweight mode (e.g., agents with certificate verification disabled) may silently accept the uncertified response, constituting a forged or stale certified response delivered under constrained conditions. This matches: **Medium ($200–$2,000) — forged or stale certified response accepted only under constrained conditions**.

## Likelihood Explanation
Exploitation requires matching `canister_id`, `sender`, `method_name`, `ingress_expiry`, and `arg` across two different request types. The `ingress_expiry` field (a nanosecond-precision timestamp set by the client) is the primary constraint: an attacker pre-populating the cache must predict or match the victim's exact value. For the `QueryV2`/`QueryV3` collision the scenario is more realistic — the same agent library may issue both types for the same logical call, or a client upgrading from v2 to v3 may reuse identical parameters within the same expiry window. For `QuerySubnetV3`, the management canister (`ic_00`) is a plausible common target where identical method/arg combinations appear across both canister and subnet query paths. The attack is repeatable once the `ingress_expiry` window is known or guessable.

## Recommendation
Add `request_type` to both `Hash` and `PartialEq` for `RequestContext` in `rs/boundary_node/ic_boundary/src/routes.rs`:

```rust
// In Hash impl
self.request_type.hash(state);

// In PartialEq impl
&& self.request_type == other.request_type
```

`RequestType` already derives `Hash`, `PartialEq`, and `Eq` (lines 26–41 of `http/mod.rs`), so no additional trait implementations are needed.

## Proof of Concept
Using the existing test harness in `cache.rs` (`gen_request_with_params`):

```rust
// 1. Send QueryV2 with (CANISTER_1, anonymous, ingress_expiry=0, arg=[1,2,3,4])
//    → assert CacheStatus::Miss
// 2. Send QueryV3 with identical parameters (same canister, sender, expiry, arg)
//    → assert CacheStatus::Hit  ← confirms cross-type collision
// 3. Assert response body equals the QueryV2 response, not a QueryV3 certified envelope
```

A second call to `gen_request_with_params` with `RequestType::QueryV3` (or `RequestType::QuerySubnetV3`) and all other parameters identical to the first call will immediately reproduce the `CacheStatus::Hit`, confirming the collision without any network access or mainnet interaction.