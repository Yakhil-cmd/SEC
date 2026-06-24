### Title
Unchecked Empty `Vec<EnvelopePair>` Index Panic in `construction_parse` — (`rs/rosetta-api/icp/src/request_handler/construction_parse.rs`)

### Summary

An unprivileged HTTP client can POST a crafted CBOR-hex `SignedTransaction` to `/construction/parse` with `signed=true` where one `(RequestType, Vec<EnvelopePair>)` tuple contains an empty `Vec<EnvelopePair>`. The handler performs an unchecked `updates[0]` index access, causing a Rust panic. No authentication is required. The path is fully concrete and locally testable.

### Finding Description

`SignedTransaction` is defined as:

```rust
pub struct SignedTransaction {
    pub requests: Vec<Request>,
}
pub type Request = (RequestType, Vec<EnvelopePair>);
``` [1](#0-0) 

The `TryFrom<ConstructionParseRequest> for ParsedTransaction` implementation deserializes the CBOR blob with no structural validation — it only checks that `serde_cbor::from_slice` succeeds: [2](#0-1) 

After deserialization, `construction_parse` iterates over `signed_transaction.requests` and unconditionally indexes `updates[0]`:

```rust
.map(
    |(request_type, updates)| match updates[0].update.content.clone() {
        HttpCallContent::Call { update } => (request_type.clone(), update),
    },
)
``` [3](#0-2) 

There is no guard that `updates` is non-empty before this access. A `SignedTransaction` with `requests: vec![(RequestType::Send, vec![])]` is valid CBOR and passes deserialization, but causes an out-of-bounds panic at line 42.

The endpoint is registered on the public actix-web HTTP server with no authentication: [4](#0-3) 

### Impact Explanation

The panic propagates through the actix-web handler. In actix-web 4.x on tokio, a panic in a synchronous function called from an async handler unwinds through the task. Depending on actix-web's worker configuration, this either:
- Aborts the request task and drops the connection (client receives a TCP reset instead of a proper HTTP error), or
- Crashes the worker thread, reducing server capacity.

Either way, the invariant that public HTTP endpoints must handle malformed input without panicking is violated. An attacker can repeatedly trigger this to degrade or disrupt the Rosetta server's availability. The claimed "full process crash" is the worst-case scenario; at minimum it is a reliable per-request DoS with improper error handling.

### Likelihood Explanation

- No authentication or special privileges required.
- The endpoint is publicly accessible.
- Crafting the payload requires only basic CBOR serialization knowledge.
- The `SignedTransaction` struct has no minimum-length invariant on the inner `Vec<EnvelopePair>`, so `serde_cbor` will accept the malformed input without error.

### Recommendation

Replace the unchecked `updates[0]` with a bounds-checked access that returns an `ApiError` on empty input:

```rust
let first = updates.first().ok_or_else(|| {
    ApiError::invalid_request("SignedTransaction request has no envelope pairs")
})?;
match first.update.content.clone() { ... }
```

Additionally, add a validation step in `TryFrom<ConstructionParseRequest> for ParsedTransaction` (or at the start of `construction_parse`) that rejects any `SignedTransaction` where any `Vec<EnvelopePair>` is empty.

### Proof of Concept

```python
import serde_cbor  # pseudocode; use any CBOR library
import requests

# Encode SignedTransaction { requests: [(RequestType::Send, [])] }
# RequestType::Send serializes as the string "TRANSACTION" or its CBOR tag
payload = cbor_encode({"requests": [["TRANSACTION", []]]})
hex_tx = payload.hex()

r = requests.post("http://<rosetta-host>/construction/parse", json={
    "network_identifier": {"blockchain": "Internet Computer", "network": "<network_id>"},
    "signed": True,
    "transaction": hex_tx
})
# Expected (correct): HTTP 400 with ApiError
# Actual: panic at updates[0], connection reset or 500 with no body
assert r.status_code != 200
``` [5](#0-4) [2](#0-1)

### Citations

**File:** rs/rosetta-api/icp/src/models.rs (L32-58)
```rust
#[derive(Clone, Eq, PartialEq, Debug, Deserialize, Serialize)]
pub struct SignedTransaction {
    pub requests: Vec<Request>,
}

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
}
impl std::fmt::Display for SignedTransaction {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", hex::encode(serde_cbor::to_vec(self).unwrap()))
    }
}

/// A vector of update/read-state calls for different ingress windows
/// of the same call.
pub type Request = (RequestType, Vec<EnvelopePair>);
```

**File:** rs/rosetta-api/icp/src/models.rs (L180-196)
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
```

**File:** rs/rosetta-api/icp/src/request_handler/construction_parse.rs (L37-48)
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
            ParsedTransaction::Unsigned(unsigned_transaction) => unsigned_transaction.updates,
        };
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
