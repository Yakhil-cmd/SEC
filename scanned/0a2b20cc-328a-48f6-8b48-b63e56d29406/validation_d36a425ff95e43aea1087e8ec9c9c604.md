### Title
Unbounded Index Access on Empty `Vec<EnvelopePair>` Causes Rosetta Node Panic — (`rs/rosetta-api/icp/src/request_handler/construction_hash.rs`)

---

### Summary

An unprivileged HTTP client can crash the ICP Rosetta node by sending a crafted `POST /construction/hash` request whose `signed_transaction` field deserializes to a `SignedTransaction` containing a `(RequestType::Send, vec![])` entry. The handler unconditionally indexes `envelope_pairs[0]` after finding a transfer-type request, with no bounds check, causing a Rust index-out-of-bounds panic.

---

### Finding Description

`construction_hash` in `rs/rosetta-api/icp/src/request_handler/construction_hash.rs` deserializes the caller-supplied `signed_transaction` string via `SignedTransaction::from_str`, which performs a plain `serde_cbor::from_slice` with no structural validation: [1](#0-0) 

The `Request` type is a plain tuple alias with no invariant enforcement: [2](#0-1) 

After deserialization, the handler searches for the first entry whose `RequestType` satisfies `is_transfer()`: [3](#0-2) 

`RequestType::Send` unconditionally returns `true` from `is_transfer()`: [4](#0-3) 

When the attacker supplies `(RequestType::Send, vec![])`, the `find` succeeds, `envelope_pairs` is an empty slice, and `envelope_pairs[0]` panics with an index-out-of-bounds, crashing the Rosetta node process.

The same unchecked `updates[0]` pattern also exists in `construction_parse`: [5](#0-4) 

---

### Impact Explanation

The Rosetta node process panics and terminates. Any operator or exchange relying on the ICP Rosetta node for ledger monitoring, balance queries, or transaction submission loses availability until the process is restarted. Because the endpoint requires no authentication and the payload is trivially constructable, the attack can be repeated immediately after each restart, constituting a sustained DoS.

---

### Likelihood Explanation

The `POST /construction/hash` endpoint is a public, unauthenticated HTTP API. Crafting the malicious payload requires only the ability to CBOR-encode a two-element tuple `("TRANSACTION", [])` and hex-encode it — a trivial operation with any CBOR library. No privileged access, key material, or protocol-level participation is required.

---

### Recommendation

Replace the unchecked `envelope_pairs[0]` index with a bounds-checked accessor and return an `ApiError` on failure:

```rust
let first = envelope_pairs.first().ok_or_else(|| {
    ApiError::invalid_transaction("Transfer request has no envelope pairs")
})?;
TransactionIdentifier::try_from_envelope(request_type.clone(), &first.update)
```

Apply the same fix to the `updates[0]` access in `construction_parse.rs`.

---

### Proof of Concept

```rust
// Craft: SignedTransaction { requests: [(RequestType::Send, vec![])] }
// RequestType::Send serializes as "TRANSACTION" (serde rename)
let malicious: Vec<(String, Vec<()>)> = vec![("TRANSACTION".to_string(), vec![])];
let cbor = serde_cbor::to_vec(&malicious).unwrap();
let hex_payload = hex::encode(&cbor);

// POST /construction/hash
// { "network_identifier": {...}, "signed_transaction": "<hex_payload>" }
// => Rosetta node panics: index out of bounds: the len is 0 but the index is 0
```

Sending this request to a live Rosetta node will cause an immediate process crash with no authentication required.

### Citations

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

**File:** rs/rosetta-api/icp/src/models.rs (L58-58)
```rust
pub type Request = (RequestType, Vec<EnvelopePair>);
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

**File:** rs/rosetta-api/icp/src/request_types.rs (L137-139)
```rust
    pub const fn is_transfer(&self) -> bool {
        matches!(self, RequestType::Send)
    }
```

**File:** rs/rosetta-api/icp/src/request_handler/construction_parse.rs (L38-46)
```rust
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
