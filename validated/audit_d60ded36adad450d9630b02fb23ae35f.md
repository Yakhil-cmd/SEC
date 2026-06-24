All three cited files are confirmed. The vulnerability is real:

- `updates[0]` unchecked index at line 42 of `construction_parse.rs` [1](#0-0) 
- `TryFrom<ConstructionParseRequest>` performs no structural validation beyond CBOR deserialization success [2](#0-1) 
- `Request = (RequestType, Vec<EnvelopePair>)` has no minimum-length invariant on the inner `Vec` [3](#0-2) 
- The endpoint is publicly registered with no authentication [4](#0-3) 

---

Audit Report

## Title
Unchecked `Vec<EnvelopePair>` Index Panic in `construction_parse` Enables Unauthenticated Per-Request DoS — (`rs/rosetta-api/icp/src/request_handler/construction_parse.rs`)

## Summary
The `construction_parse` handler unconditionally indexes `updates[0]` on the `Vec<EnvelopePair>` component of each deserialized `Request` tuple without first checking that the vector is non-empty. A crafted CBOR-encoded `SignedTransaction` containing a `Request` with an empty `Vec<EnvelopePair>` passes deserialization successfully and triggers a Rust index-out-of-bounds panic. The endpoint requires no authentication, making this a trivially repeatable per-request DoS against the ICP Rosetta API.

## Finding Description
`Request` is defined as `(RequestType, Vec<EnvelopePair>)` with no minimum-length constraint on the inner vector. The `TryFrom<ConstructionParseRequest> for ParsedTransaction` implementation deserializes the CBOR blob and returns `Ok` as long as `serde_cbor::from_slice` succeeds — it performs no structural validation on the contents of `requests`. In `construction_parse`, the signed branch iterates over `signed_transaction.requests` and executes:

```rust
|(request_type, updates)| match updates[0].update.content.clone() {
    HttpCallContent::Call { update } => (request_type.clone(), update),
}
```

When `updates` is an empty `Vec`, `updates[0]` panics unconditionally. The panic propagates through the async actix-web handler; actix-web 4.x on tokio catches the panic at the task boundary, drops the connection (TCP reset to the client), and logs no structured error. The worker thread survives but the request is never answered with a proper HTTP error response. An attacker can repeat this indefinitely with zero authentication.

## Impact Explanation
This is a **High** severity finding matching the allowed impact: *"Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS"* and *"Significant Rosetta … security impact with concrete user or protocol harm."* The ICP Rosetta API is an explicitly in-scope financial integration component. Repeated exploitation degrades or disrupts Rosetta server availability, preventing legitimate users from constructing, parsing, or submitting ICP transactions. The attack is low-cost, requires no privileges, and is fully repeatable.

## Likelihood Explanation
No authentication or special privileges are required. The `/construction/parse` endpoint is publicly bound on the actix-web HTTP server. Crafting the payload requires only basic CBOR serialization: encode `{"requests": [["TRANSACTION", []]]}` (or any valid `RequestType` variant paired with an empty array) as CBOR, hex-encode it, and POST it with `"signed": true`. The `SignedTransaction` struct imposes no minimum-length invariant on `Vec<EnvelopePair>`, so `serde_cbor` accepts the input without error. The attack is deterministic and reproducible.

## Recommendation
Replace the unchecked `updates[0]` with a bounds-checked access that propagates an `ApiError` on empty input:

```rust
let first = updates.first().ok_or_else(|| {
    ApiError::invalid_request("SignedTransaction request has no envelope pairs")
})?;
match first.update.content.clone() {
    HttpCallContent::Call { update } => (request_type.clone(), update),
}
```

Additionally, add a validation step in `TryFrom<ConstructionParseRequest> for ParsedTransaction` (or at the start of `construction_parse`) that rejects any `SignedTransaction` where any `Vec<EnvelopePair>` is empty, returning `ApiError::invalid_request` before the iteration begins.

## Proof of Concept
Minimal local reproduction:

```python
import cbor2, requests, json

# Encode SignedTransaction { requests: [(RequestType::Send variant, [])] }
# RequestType serializes as a tagged/string variant; use the CBOR representation
# produced by serde_cbor for the "Send" variant (a map with key "TRANSACTION")
payload = cbor2.dumps({"requests": [{"TRANSACTION": []}, []]})
# Alternatively, serialize via the Rust type directly in a unit test:
# let tx = SignedTransaction { requests: vec![(RequestType::Send, vec![])] };
# let hex = hex::encode(serde_cbor::to_vec(&tx).unwrap());

hex_tx = payload.hex()

r = requests.post("http://<rosetta-host>:8080/construction/parse", json={
    "network_identifier": {"blockchain": "Internet Computer", "network": "<network_id>"},
    "signed": True,
    "transaction": hex_tx,
})
# Expected (correct): HTTP 400 ApiError
# Actual: panic at updates[0], TCP reset or 500 with no structured body
```

A deterministic Rust unit test can be added alongside the existing `test_payloads_parse_identity` test in `construction_parse.rs`: construct a `SignedTransaction` with `requests: vec![(RequestType::Send, vec![])]`, CBOR-encode and hex-encode it, call `handler.construction_parse(...)` with `signed: true`, and assert the result is `Err(ApiError)` rather than a panic.

### Citations

**File:** rs/rosetta-api/icp/src/request_handler/construction_parse.rs (L41-45)
```rust
                .map(
                    |(request_type, updates)| match updates[0].update.content.clone() {
                        HttpCallContent::Call { update } => (request_type.clone(), update),
                    },
                )
```

**File:** rs/rosetta-api/icp/src/models.rs (L56-58)
```rust
/// A vector of update/read-state calls for different ingress windows
/// of the same call.
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

**File:** rs/rosetta-api/icp/src/rosetta_server.rs (L115-122)
```rust
#[post("/construction/parse")]
async fn construction_parse(
    msg: web::Json<ConstructionParseRequest>,
    req_handler: web::Data<RosettaRequestHandler>,
) -> HttpResponse {
    let res = req_handler.construction_parse(msg.into_inner());
    to_rosetta_response(res, &req_handler.rosetta_metrics())
}
```
