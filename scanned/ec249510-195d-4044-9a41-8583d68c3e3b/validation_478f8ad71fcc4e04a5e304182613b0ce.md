### Title
Unauthenticated DoS via Index-Out-of-Bounds Panic in `construction_hash` — (`rs/rosetta-api/icp/src/request_handler/construction_hash.rs`)

---

### Summary

`construction_hash` performs an unchecked index `envelope_pairs[0]` on a `Vec<EnvelopePair>` that is fully attacker-controlled via CBOR deserialization. An empty vec with a `RequestType::Send` entry causes an unconditional panic, crashing the Rosetta process.

---

### Finding Description

In `construction_hash`, after `SignedTransaction::from_str` successfully deserializes attacker-supplied CBOR, the code finds the first transfer-type request and immediately indexes into its envelope list without any length guard:

```rust
// construction_hash.rs lines 18-28
let transaction_identifier = if let Some((request_type, envelope_pairs)) =
    signed_transaction
        .requests
        .iter()
        .rev()
        .find(|(rt, _)| rt.is_transfer())
{
    TransactionIdentifier::try_from_envelope(
        request_type.clone(),
        &envelope_pairs[0].update,   // ← panics if vec is empty
    )
``` [1](#0-0) 

The `Request` type alias is `(RequestType, Vec<EnvelopePair>)`, so the inner `Vec` can be empty: [2](#0-1) 

`RequestType::is_transfer()` returns `true` for `RequestType::Send`: [3](#0-2) 

`SignedTransaction::from_str` is pure CBOR deserialization with no structural validation — an empty `Vec<EnvelopePair>` is valid CBOR and will deserialize without error: [4](#0-3) 

There is no length check anywhere between deserialization and the `[0]` access. Notably, the `TryFrom<&models::Request> for Request` conversion in `request.rs` **does** handle this correctly with `.first().ok_or_else(...)`, but `construction_hash` bypasses that path entirely: [5](#0-4) 

---

### Impact Explanation

A single unauthenticated HTTP POST to `/construction/hash` with a crafted payload crashes the Rosetta process. The endpoint requires no authentication. The Rosetta node is a critical infrastructure component used by exchanges and financial integrations for ICP transfers. Repeated single-packet requests can keep the process permanently unavailable.

---

### Likelihood Explanation

The endpoint is publicly reachable, requires no credentials, and the exploit requires only constructing a valid CBOR structure with an empty array — trivially achievable with any CBOR library. No brute force, no timing dependency, no privileged access.

---

### Recommendation

Replace the unchecked `[0]` index with `.first()` and propagate an `ApiError` on `None`, consistent with how `TryFrom<&models::Request> for Request` already handles this case:

```rust
let envelope = envelope_pairs
    .first()
    .ok_or_else(|| ApiError::invalid_request("No envelope pairs in transfer request"))?;
TransactionIdentifier::try_from_envelope(request_type.clone(), &envelope.update)
``` [6](#0-5) 

---

### Proof of Concept

```rust
use ic_rosetta_api::models::{SignedTransaction};
use ic_rosetta_api::request_types::RequestType;

let crafted = SignedTransaction {
    requests: vec![(RequestType::Send, vec![])],  // empty EnvelopePair vec
};
let hex = format!("{}", crafted);  // CBOR-encode + hex

// POST to /construction/hash:
// { "network_identifier": {...}, "signed_transaction": "<hex>" }
// → construction_hash panics at envelope_pairs[0] → process crash
```

The `SignedTransaction::Display` impl serializes via `serde_cbor`, so the crafted struct round-trips cleanly through `from_str`. Under `panic=abort` (common in production builds) this is an immediate process termination. [7](#0-6)

### Citations

**File:** rs/rosetta-api/icp/src/request_handler/construction_hash.rs (L18-28)
```rust
        let transaction_identifier = if let Some((request_type, envelope_pairs)) =
            signed_transaction
                .requests
                .iter()
                .rev()
                .find(|(rt, _)| rt.is_transfer())
        {
            TransactionIdentifier::try_from_envelope(
                request_type.clone(),
                &envelope_pairs[0].update,
            )
```

**File:** rs/rosetta-api/icp/src/models.rs (L37-48)
```rust
impl FromStr for SignedTransaction {
    type Err = String;
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        let bytes = hex::decode(s).map_err(|err| format!("{err:?}"))?;
        serde_cbor::from_slice(bytes.as_slice()).or_else(|first_err| {
            serde_cbor::from_slice::<LegacySignedTransaction>(bytes.as_slice())
                .map(|legacy_requests| SignedTransaction {
                    requests: legacy_requests,
                })
                .map_err(|_| format!("{first_err:?}"))
        })
    }
```

**File:** rs/rosetta-api/icp/src/models.rs (L50-53)
```rust
impl std::fmt::Display for SignedTransaction {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", hex::encode(serde_cbor::to_vec(self).unwrap()))
    }
```

**File:** rs/rosetta-api/icp/src/models.rs (L58-58)
```rust
pub type Request = (RequestType, Vec<EnvelopePair>);
```

**File:** rs/rosetta-api/icp/src/request_types.rs (L137-139)
```rust
    pub const fn is_transfer(&self) -> bool {
        matches!(self, RequestType::Send)
    }
```

**File:** rs/rosetta-api/icp/src/request.rs (L236-239)
```rust
        let (request_type, calls) = req;
        let payload: &models::EnvelopePair = calls
            .first()
            .ok_or_else(|| ApiError::invalid_request("No request payload provided."))?;
```
