### Title
Unbounded CBOR Deserialization in ICRC1 Rosetta Construction API Enables Heap Exhaustion DoS — (`rs/rosetta-api/icrc1/src/construction_api/types.rs`)

---

### Summary

The ICRC1 Rosetta server exposes several unauthenticated HTTP endpoints that accept a hex-encoded CBOR blob as a JSON string field. Before calling `serde_cbor::from_slice`, no size cap is enforced on either the HTTP body or the decoded CBOR bytes. An attacker can POST a crafted payload containing a `Vec<Envelope>` or `Vec<EnvelopeContent>` with a large number of entries, triggering unbounded heap allocation and crashing the Rosetta process.

---

### Finding Description

`SignedTransaction::from_str` and `UnsignedTransaction::from_str` in the ICRC1 construction API directly call `serde_cbor::from_slice` on attacker-controlled bytes with no preceding size check: [1](#0-0) [2](#0-1) 

These functions are called directly from the following public HTTP endpoints, all reachable without authentication:

- `/construction/parse` → `construction_parse` → `SignedTransaction::from_str` / `UnsignedTransaction::from_str`
- `/construction/hash` → `construction_hash` → `SignedTransaction::from_str`
- `/construction/submit` → `construction_submit` → `SignedTransaction::from_str`
- `/construction/combine` → `construction_combine` → `UnsignedTransaction::from_str` [3](#0-2) [4](#0-3) 

The axum router in `main.rs` registers these routes with **no** `RequestBodyLimitLayer` and **no** JSON body size configuration: [5](#0-4) 

A grep for `RequestBodyLimitLayer`, `body_limit`, `MAX_REQUEST`, `JsonConfig`, and `.limit(` across the entire `rs/rosetta-api/icrc1/` tree returns **zero matches**. This contrasts sharply with the ICP Rosetta server, which explicitly sets a 4 MB JSON body limit via actix-web's `JsonConfig`: [6](#0-5) 

The deserialized types hold unbounded `Vec` fields:

- `SignedTransaction.envelopes: Vec<Envelope<'a>>` — each `Envelope` contains an `EnvelopeContent` with multiple heap-allocated fields (`sender`, `canister_id`, `method_name`, `arg`, etc.)
- `UnsignedTransaction.envelope_contents: Vec<EnvelopeContent>` [7](#0-6) [8](#0-7) 

`serde_cbor` reads the declared array length and deserializes each element sequentially. An attacker who provides N minimal but structurally valid `EnvelopeContent::Call` entries causes O(N) heap allocations before any application-level validation runs.

---

### Impact Explanation

A single unauthenticated HTTP POST to `/construction/parse` or `/construction/hash` with a crafted payload can exhaust the heap of the ICRC1 Rosetta process, causing an OOM kill or unrecoverable panic. This takes the Rosetta API offline for all users of that deployment, blocking all transaction construction, submission, and hash queries until the process is restarted. The impact is a non-volumetric, single-request denial of service against the Rosetta process.

---

### Likelihood Explanation

The endpoint is publicly reachable with no authentication. The attack requires only knowledge of CBOR encoding (well-documented) and a single HTTP request. No privileged access, key material, or network-level attack is needed. The missing body-size guard is a straightforward omission that is trivially exploitable.

---

### Recommendation

1. Apply `axum::extract::DefaultBodyLimit` or `tower_http::limit::RequestBodyLimitLayer` to all construction routes, capping the request body at a reasonable maximum (e.g., 4 MB, matching the ICP Rosetta server).
2. Add an explicit byte-length check on the hex-decoded CBOR slice before calling `serde_cbor::from_slice` in both `SignedTransaction::from_str` and `UnsignedTransaction::from_str`.
3. Consider using `serde_cbor`'s `from_reader` with a `Read` adapter that enforces a byte limit, or switch to a deserializer that supports configurable recursion/allocation limits.

---

### Proof of Concept

```python
import cbor2, binascii, requests, json

# Craft a CBOR array of 500_000 minimal EnvelopeContent::Call entries
# Each entry is a CBOR map with the required fields
minimal_call = {
    0: "call",                          # type tag
    "request_type": "call",
    "sender": b'\x04',                  # anonymous principal
    "canister_id": b'\x00' * 10,
    "method_name": "x",
    "arg": b'',
    "ingress_expiry": 9999999999999999999,
}
payload = cbor2.dumps({"envelope_contents": [minimal_call] * 500_000})
hex_payload = binascii.hexlify(payload).decode()

body = {
    "network_identifier": {"blockchain": "Internet Computer", "network": "<ledger_id>"},
    "transaction": hex_payload,
    "signed": False,
}
# Single request → heap exhaustion → Rosetta process OOM
r = requests.post("http://<rosetta-host>:8080/construction/parse", json=body, timeout=120)
print(r.status_code)
```

Fuzzing `SignedTransaction::from_str` and `UnsignedTransaction::from_str` with crafted CBOR payloads containing large envelope counts and asserting that memory usage stays bounded would confirm the absence of any guard. [1](#0-0)

### Citations

**File:** rs/rosetta-api/icrc1/src/construction_api/types.rs (L45-48)
```rust
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct SignedTransaction<'a> {
    pub envelopes: Vec<Envelope<'a>>,
}
```

**File:** rs/rosetta-api/icrc1/src/construction_api/types.rs (L56-61)
```rust
impl FromStr for SignedTransaction<'_> {
    type Err = anyhow::Error;
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        serde_cbor::from_slice(hex::decode(s)?.as_slice()).map_err(|err| anyhow!("{:?}", err))
    }
}
```

**File:** rs/rosetta-api/icrc1/src/construction_api/types.rs (L144-147)
```rust
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct UnsignedTransaction {
    pub envelope_contents: Vec<EnvelopeContent>,
}
```

**File:** rs/rosetta-api/icrc1/src/construction_api/types.rs (L155-160)
```rust
impl FromStr for UnsignedTransaction {
    type Err = anyhow::Error;
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        serde_cbor::from_slice(hex::decode(s)?.as_slice()).map_err(|err| anyhow!("{:?}", err))
    }
}
```

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L79-110)
```rust
pub async fn construction_submit(
    signed_transaction: String,
    icrc1_ledger_id: CanisterId,
    icrc1_agent: Arc<Icrc1Agent>,
) -> Result<ConstructionSubmitResponse, Error> {
    let signed_transaction = SignedTransaction::from_str(&signed_transaction)
        .map_err(|err| Error::parsing_unsuccessful(&err))?;

    handle_construction_submit(signed_transaction, icrc1_ledger_id.into(), icrc1_agent)
        .await
        .map_err(|err| Error::processing_construction_failed(&err))
}

pub fn construction_hash(signed_transaction: String) -> Result<ConstructionHashResponse, Error> {
    let signed_transaction = SignedTransaction::from_str(&signed_transaction)
        .map_err(|err| Error::parsing_unsuccessful(&err))?;

    handle_construction_hash(signed_transaction)
        .map_err(|err| Error::processing_construction_failed(&err))
}

pub fn construction_combine(
    unsigned_transaction: String,
    signatures: Vec<Signature>,
) -> Result<ConstructionCombineResponse, Error> {
    let unsigned_transaction = UnsignedTransaction::from_str(&unsigned_transaction)
        .map_err(|err| Error::parsing_unsuccessful(&err))?;

    handle_construction_combine(unsigned_transaction, signatures)
        .map_err(|err| Error::processing_construction_failed(&err))
}

```

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L196-221)
```rust
pub fn construction_parse(
    transaction_string: String,
    transaction_is_signed: bool,
    currency: Currency,
) -> Result<ConstructionParseResponse, Error> {
    let (ingress_expiry_start, ingress_expiry_end, envelope_contents) = if transaction_is_signed {
        let signed_transaction = SignedTransaction::from_str(&transaction_string)
            .map_err(|err| Error::parsing_unsuccessful(&err))?;
        (
            signed_transaction.get_lowest_ingress_expiry(),
            signed_transaction.get_highest_ingress_expiry(),
            signed_transaction
                .envelopes
                .into_iter()
                .map(|envelope| envelope.content.into_owned())
                .collect(),
        )
    } else {
        let unsigned_transaction = UnsignedTransaction::from_str(&transaction_string)
            .map_err(|err| Error::parsing_unsuccessful(&err))?;
        (
            unsigned_transaction.get_lowest_ingress_expiry(),
            unsigned_transaction.get_highest_ingress_expiry(),
            unsigned_transaction.envelope_contents,
        )
    };
```

**File:** rs/rosetta-api/icrc1/src/main.rs (L380-391)
```rust
        .route("/construction/payloads", post(construction_payloads))
        .route("/construction/parse", post(construction_parse))
        // Apply the metrics middleware
        .layer(metrics_layer)
        // This layer creates a span for each http request and attaches
        // the request_id, HTTP Method and path to it.
        .layer(add_request_span())
        // This layer creates a new id for each request and puts it into the
        // request extensions. Note that it should be added after the
        // Trace layer.
        .layer(RequestIdLayer)
        .with_state(token_app_states.clone());
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
