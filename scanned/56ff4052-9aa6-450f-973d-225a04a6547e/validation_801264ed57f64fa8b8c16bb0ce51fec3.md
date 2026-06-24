### Title
Unbounded O(N×M) Amplification in `construction_combine` Nested Loop Causes Rosetta Node Memory Exhaustion — (`rs/rosetta-api/icp/src/request_handler/construction_combine.rs`)

---

### Summary

The `construction_combine` handler contains a nested loop over `updates` (N) × `ingress_expiries` (M) with no bounds on either dimension. An unprivileged attacker can craft a single ≤4 MB JSON request that triggers tens of millions of loop iterations, each performing DER encoding, SHA-256 hashing, heap allocations, and `Vec` pushes. The output `requests` vector can grow to tens of gigabytes, exhausting the Rosetta process's memory and/or saturating its CPU.

---

### Finding Description

The outer loop iterates over `unsigned_transaction.updates` (N entries) and the inner loop over `unsigned_transaction.ingress_expiries` (M entries): [1](#0-0) 

`UnsignedTransaction` is a plain CBOR-deserialized struct with no size constraints on either field: [2](#0-1) 

Each inner-loop iteration performs:
- `update.clone()` (cloning an `HttpCanisterUpdate` with multiple `Vec<u8>` fields) [3](#0-2) 
- `make_read_state_from_update` (SHA-256 hashing) [4](#0-3) 
- Two `der_encode_pk` / `hex_decode_pk` round-trips (once for the call envelope, once for the read-state envelope) [5](#0-4) 
- Two `HttpRequestEnvelope` heap allocations pushed into `request_envelopes` [6](#0-5) 

**Amplification trick — only 2×M signatures needed for N×M iterations:**

`update.id()` is computed from `representation_independent_hash`, which includes `ingress_expiry` but not the position in the `updates` vector: [7](#0-6) 

If the attacker crafts N *identical* `HttpCanisterUpdate` entries (same canister_id, method, arg, sender, nonce), all N copies produce the same `update.id()` for a given expiry. The HashMap lookup at line 50–51 therefore succeeds for all N updates using only M distinct transaction signatures and M distinct read-state signatures — 2×M total signatures in the request body. [8](#0-7) 

**Budget within the 4 MB body limit** (actix-web `JsonConfig::limit`): [9](#0-8) 

| Allocation | Size estimate |
|---|---|
| 2×M signatures (M=1 000, ~250 B each) | ~500 KB |
| N identical updates in CBOR+hex (N=28 000, ~120 B each) | ~3.4 MB |
| M ingress_expiries (1 000 × 16 B hex) | ~16 KB |

This yields **N×M ≈ 28 million iterations**. Each `EnvelopePair` output is ~510 bytes, so the `requests` vector alone consumes **~14 GB of heap** before the response is serialized.

There is no concurrency limit on the `/construction/combine` route: [10](#0-9) 

---

### Impact Explanation

A single crafted request OOMs the Rosetta process (or saturates one CPU core for minutes before OOM). Because the Rosetta node is a single-process service with no request-level resource accounting on this endpoint, one request is sufficient to render the node unavailable. Impact is scoped to the Rosetta node — the IC replica and consensus are unaffected.

---

### Likelihood Explanation

The endpoint is unauthenticated and publicly reachable. The attack requires only the ability to send an HTTP POST to `/construction/combine`. No key material, governance access, or network-level privilege is needed. The crafted payload is trivially constructable.

---

### Recommendation

1. **Add a hard cap on `updates` and `ingress_expiries` lengths** immediately after deserialization (e.g., `updates.len() ≤ 50`, `ingress_expiries.len() ≤ 200`, matching the legitimate ~24 h window at ~5-minute intervals).
2. **Add a product cap**: reject if `updates.len() * ingress_expiries.len() > SOME_LIMIT` (e.g., 10 000).
3. **Move DER encoding outside the inner loop**: the public key is the same for every iteration; decode and encode it once before the loops begin.
4. Consider adding a per-request timeout or memory budget at the actix-web middleware layer for construction endpoints.

---

### Proof of Concept

```python
import cbor2, json, requests, os

# Minimal HttpCanisterUpdate (CBOR map matching ic_types field names)
update = {
    "canister_id": b"\x00" * 8,
    "method_name": "x",
    "arg": b"",
    "sender": b"\x04",
    "ingress_expiry": 0,
    "request_type": "call",
}

N = 28_000   # identical updates
M = 1_000    # ingress expiries

unsigned_tx = {
    "updates": [["Send", update]] * N,
    "ingress_expiries": list(range(M)),
}
unsigned_tx_hex = cbor2.dumps(unsigned_tx).hex()

# 2*M fake signatures (same sig_data for all N updates per expiry)
# In a real attack, compute actual sig_data hashes; here we show structure.
signatures = []
for _ in range(2 * M):
    signatures.append({
        "signing_payload": {"hex_bytes": "aa" * 32},
        "public_key": {"hex_bytes": "bb" * 32, "curve_type": "edwards25519"},
        "hex_bytes": "cc" * 64,
        "signature_type": "ed25519",
    })

payload = {
    "network_identifier": {"blockchain": "Internet Computer", "network": "00"},
    "unsigned_transaction": unsigned_tx_hex,
    "signatures": signatures,
}

# Single request → Rosetta OOM
requests.post("http://<rosetta-node>:8080/construction/combine",
              json=payload, timeout=300)
```

### Citations

**File:** rs/rosetta-api/icp/src/request_handler/construction_combine.rs (L41-44)
```rust
        for (request_type, update) in unsigned_transaction.updates {
            let mut request_envelopes = vec![];

            for ingress_expiry in &unsigned_transaction.ingress_expiries {
```

**File:** rs/rosetta-api/icp/src/request_handler/construction_combine.rs (L45-46)
```rust
                let mut update = update.clone();
                update.ingress_expiry = *ingress_expiry;
```

**File:** rs/rosetta-api/icp/src/request_handler/construction_combine.rs (L48-48)
```rust
                let read_state = make_read_state_from_update(&update);
```

**File:** rs/rosetta-api/icp/src/request_handler/construction_combine.rs (L50-56)
```rust
                let transaction_signature = signatures_by_sig_data
                    .get(&make_sig_data(&update.id()))
                    .ok_or_else(|| {
                        ApiError::internal_error(
                            "Could not find signature for transaction".to_string(),
                        )
                    })?;
```

**File:** rs/rosetta-api/icp/src/request_handler/construction_combine.rs (L70-83)
```rust
                            Ed25519KeyPair::der_encode_pk(
                                Ed25519KeyPair::hex_decode_pk(
                                    &transaction_signature.public_key.hex_bytes,
                                )
                                .map_err(|err| {
                                    ApiError::InvalidPublicKey(
                                        false,
                                        Details::from(format!("{err:?}")),
                                    )
                                })?,
                            )
                            .map_err(|err| {
                                ApiError::InvalidPublicKey(false, Details::from(format!("{err:?}")))
                            })?,
```

**File:** rs/rosetta-api/icp/src/request_handler/construction_combine.rs (L164-167)
```rust
                request_envelopes.push(EnvelopePair {
                    update: envelope,
                    read_state: read_state_envelope,
                });
```

**File:** rs/rosetta-api/icp/src/models.rs (L252-255)
```rust
pub struct UnsignedTransaction {
    pub updates: Vec<(RequestType, HttpCanisterUpdate)>,
    pub ingress_expiries: Vec<u64>,
}
```

**File:** rs/types/types/src/messages/http.rs (L157-159)
```rust
    pub fn id(&self) -> MessageId {
        MessageId::from(self.representation_independent_hash())
    }
```

**File:** rs/rosetta-api/icp/src/rosetta_server.rs (L79-86)
```rust
#[post("/construction/combine")]
async fn construction_combine(
    msg: web::Json<ConstructionCombineRequest>,
    req_handler: web::Data<RosettaRequestHandler>,
) -> HttpResponse {
    let res = req_handler.construction_combine(msg.into_inner());
    to_rosetta_response(res, &req_handler.rosetta_metrics())
}
```

**File:** rs/rosetta-api/icp/src/rosetta_server.rs (L296-303)
```rust
                .app_data(web::Data::new(
                    web::JsonConfig::default()
                        .limit(4 * 1024 * 1024)
                        .error_handler(move |e, _| {
                            errors::convert_to_error(&ApiError::invalid_request(format!("{e:#?}")))
                                .into()
                        }),
                ))
```
