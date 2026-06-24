The code confirms the vulnerability. Here is the analysis:

**Key facts from the code:**

1. `Request` type alias: `pub type Request = (RequestType, Vec<EnvelopePair>)` — the `Vec<EnvelopePair>` has no minimum-length constraint. [1](#0-0) 

2. `ParsedTransaction::try_from` performs only CBOR deserialization — no guard checks that each `Vec<EnvelopePair>` is non-empty before returning `ParsedTransaction::Signed`. [2](#0-1) 

3. The unchecked index access `updates[0]` is inside a `.map()` closure — if any tuple has an empty `Vec<EnvelopePair>`, Rust panics with an index-out-of-bounds, crashing the process. [3](#0-2) 

4. The same pattern exists in `construction_hash.rs` at `&envelope_pairs[0].update`, confirming this is a systemic pattern. [4](#0-3) 

---

### Title
Unchecked `Vec<EnvelopePair>` index access causes process-crashing panic in `construction_parse` — (`rs/rosetta-api/icp/src/request_handler/construction_parse.rs`)

### Summary
An unprivileged API client can POST a structurally valid CBOR-encoded `SignedTransaction` containing a `(RequestType, vec![])` tuple to `/construction/parse`. The absence of a non-empty guard before `updates[0]` causes an index-out-of-bounds Rust panic, crashing the Rosetta process.

### Finding Description
`construction_parse` deserializes the attacker-supplied hex-CBOR blob into a `SignedTransaction` via `ParsedTransaction::try_from`, which only validates CBOR structure — not semantic invariants. It then immediately maps over `signed_transaction.requests` and accesses `updates[0]` without checking `!updates.is_empty()`. A `SignedTransaction { requests: vec![(RequestType::Send, vec![])] }` is valid CBOR and passes deserialization, but the subsequent `updates[0]` panics. [5](#0-4) 

### Impact Explanation
A Rust `panic!` in a non-`catch_unwind` context terminates the Rosetta process. A single unauthenticated HTTP POST is sufficient to crash the node. Repeated requests keep it unavailable, constituting a persistent, non-volumetric DoS against the Rosetta API service.

### Likelihood Explanation
The `/construction/parse` endpoint is public and unauthenticated. The malicious payload is trivial to construct: serialize `SignedTransaction { requests: vec![(RequestType::Send, vec![])] }` to CBOR and hex-encode it. No privileged access, key material, or network-level attack is required.

### Recommendation
Add an explicit non-empty check before the index access:

```rust
.map(|(request_type, updates)| {
    if updates.is_empty() {
        return Err(ApiError::invalid_request(
            "EnvelopePair list must not be empty"
        ));
    }
    match updates[0].update.content.clone() {
        HttpCallContent::Call { update } => Ok((request_type.clone(), update)),
    }
})
.collect::<Result<Vec<_>, _>>()?
```

Apply the same fix to `construction_hash.rs` line 27 where `&envelope_pairs[0].update` has the identical unguarded pattern. [6](#0-5) 

### Proof of Concept
```rust
use serde_cbor;
use hex;

// Construct the malicious payload
let malicious = SignedTransaction {
    requests: vec![(RequestType::Send, vec![])],  // empty EnvelopePair vec
};
let cbor_bytes = serde_cbor::to_vec(&malicious).unwrap();
let hex_payload = hex::encode(cbor_bytes);

// POST to Rosetta
// POST /construction/parse
// { "network_identifier": {...}, "signed": true, "transaction": "<hex_payload>" }
// -> process panics at updates[0], Rosetta crashes
```

### Citations

**File:** rs/rosetta-api/icp/src/models.rs (L58-58)
```rust
pub type Request = (RequestType, Vec<EnvelopePair>);
```

**File:** rs/rosetta-api/icp/src/models.rs (L180-197)
```rust
impl TryFrom<ConstructionParseRequest> for ParsedTransaction {
    type Error = ApiError;
    fn try_from(value: ConstructionParseRequest) -> Result<Self, Self::Error> {
        if value.signed {
            Ok(ParsedTransaction::Signed(
                serde_cbor::from_slice(&from_hex(&value.transaction)?).map_err(|e| {
                    ApiError::invalid_request(format!("Could not decode signed transaction: {e}"))
                })?,
            ))
        } else {
            Ok(ParsedTransaction::Unsigned(
                serde_cbor::from_slice(&from_hex(&value.transaction)?).map_err(|e| {
                    ApiError::invalid_request(format!("Could not decode unsigned transaction: {e}"))
                })?,
            ))
        }
    }
}
```

**File:** rs/rosetta-api/icp/src/request_handler/construction_parse.rs (L37-46)
```rust
        let updates: Vec<_> = match ParsedTransaction::try_from(msg.clone())? {
            ParsedTransaction::Signed(signed_transaction) => signed_transaction
                .requests
                .iter()
                .map(
                    |(request_type, updates)| match updates[0].update.content.clone() {
                        HttpCallContent::Call { update } => (request_type.clone(), update),
                    },
                )
                .collect(),
```

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
