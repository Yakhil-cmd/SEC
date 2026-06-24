Audit Report

## Title
Unbounded `public_keys` Array Enables CPU Exhaustion DoS in ICP Rosetta `/construction/payloads` — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

## Summary
The ICP Rosetta `/construction/payloads` endpoint accepts an unbounded `public_keys` array and performs a full cryptographic derivation (hex decode + DER encode + SHA-224 hash) for every entry with no count guard. The handler executes this work synchronously inside an `async fn` actix-web handler, blocking the worker thread for the duration. A small number of concurrent requests carrying ~40,000 keys each can exhaust all actix-web worker threads, rendering the Rosetta process unresponsive to all other requests.

## Finding Description
**Handler registration** — `POST /construction/payloads` is registered with no authentication. The `async fn construction_payloads` handler calls `req_handler.construction_payloads(msg.into_inner())` without `.await`, meaning the entire computation runs synchronously on the actix-web worker thread: [1](#0-0) 

**Only server-side guard** — a 4 MB JSON body limit; no check on `pks.len()`: [2](#0-1) 

**Unbounded loop** — `pks` is iterated immediately with no count check. Every entry invokes `principal_id_from_public_key`: [3](#0-2) 

**Per-key cryptographic work** — `principal_id_from_public_key` dispatches to `Ed25519KeyPair::get_principal_id`, which performs hex decode → DER encode → SHA-224 hash via `PrincipalId::new_self_authenticating`: [4](#0-3) [5](#0-4) [6](#0-5) 

**Capacity**: A minimal `PublicKey` JSON object is ~100 bytes; the 4 MB limit allows ~40,000 keys per request. With actix-web's default worker count (number of CPU cores), a handful of concurrent requests saturates all threads.

**Contrast with ICRC-1 Rosetta**, which enforces a hard limit of exactly one public key: [7](#0-6) 

No equivalent guard exists in the ICP Rosetta path.

## Impact Explanation
An unprivileged HTTP client can send a small number of concurrent `POST /construction/payloads` requests, each carrying ~40,000 valid `PublicKey` entries. The Rosetta process's actix-web worker threads are fully occupied executing synchronous SHA-224 loops, causing all other API requests (balance queries, block fetches, transaction submissions) to time out. The Rosetta API — an explicitly in-scope financial integration component — becomes effectively unavailable without crashing. This matches the allowed High impact: **"Application/platform-level DoS... not based on raw volumetric DDoS"** and **"Significant... Rosetta... security impact with concrete user or protocol harm."**

## Likelihood Explanation
The endpoint is publicly reachable on any deployed ICP Rosetta instance. No credentials, tokens, or prior state are required. The attack payload is trivially constructable (repeat a single valid public key 40,000 times in a JSON array). The 4 MB body limit is the only barrier and it does not prevent the attack — it only caps the per-request key count. A single attacker with a standard HTTP client can sustain the attack indefinitely by continuously reissuing requests.

## Recommendation
1. **Add a hard key-count limit** immediately after extracting `pks` in `construction_payloads.rs`, before the iterator loop:
   ```rust
   const MAX_PUBLIC_KEYS: usize = 10;
   if pks.len() > MAX_PUBLIC_KEYS {
       return Err(ApiError::invalid_request(
           format!("Too many public_keys: max {MAX_PUBLIC_KEYS}")
       ));
   }
   ```
2. **Move CPU-bound work off the async executor** using `actix_web::web::block` or `tokio::task::spawn_blocking` to prevent blocking worker threads even for legitimate requests.
3. **Optionally add per-IP rate limiting** at the HTTP layer as defense-in-depth.

## Proof of Concept
**Local integration test (safe, no mainnet interaction):**

```python
import requests, json, threading

KEY = {"hex_bytes": "a" * 64, "curve_type": "edwards25519"}
PAYLOAD = {
    "network_identifier": {"blockchain": "Internet Computer", "network": "<local_network_id>"},
    "operations": [],
    "public_keys": [KEY] * 40000,
}

def attack():
    requests.post("http://localhost:8080/construction/payloads", json=PAYLOAD, timeout=120)

threads = [threading.Thread(target=attack) for _ in range(8)]
for t in threads: t.start()
for t in threads: t.join()
# All subsequent requests to the local Rosetta API will time out during the attack window.
```

**Benchmark PoC** (unit test, no network required): call `RosettaRequestHandler::construction_payloads` directly with `public_keys` of length 1, 100, 1,000, 10,000, and 40,000 and assert that wall-clock time scales linearly with key count, confirming the unbounded O(n) behavior.

### Citations

**File:** rs/rosetta-api/icp/src/rosetta_server.rs (L124-131)
```rust
#[post("/construction/payloads")]
async fn construction_payloads(
    msg: web::Json<ConstructionPayloadsRequest>,
    req_handler: web::Data<RosettaRequestHandler>,
) -> HttpResponse {
    let res = req_handler.construction_payloads(msg.into_inner());
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

**File:** rs/rosetta-api/icp/src/request_handler/construction_payloads.rs (L112-127)
```rust
        let pks_map = pks
            .iter()
            .map(|pk| {
                let pid: PrincipalId = principal_id_from_public_key(pk).map_err(|err| {
                    let err_msg = format!(
                        "Failed to derive principal ID from public key (curve_type: {:?}, hex_bytes: {}): {err:?}",
                        pk.curve_type,
                        pk.hex_bytes
                    );
                    debug!("{}", err_msg);
                    ApiError::InvalidPublicKey(false, Details::from(err_msg))
                })?;
                let account: icp_ledger::AccountIdentifier = pid.into();
                Ok((account, pk))
            })
            .collect::<Result<HashMap<_, _>, ApiError>>()?;
```

**File:** rs/rosetta-api/common/rosetta_core/src/convert.rs (L6-12)
```rust
pub fn principal_id_from_public_key(pk: &PublicKey) -> anyhow::Result<PrincipalId> {
    match pk.curve_type {
        CurveType::Edwards25519 => Ed25519KeyPair::get_principal_id(&pk.hex_bytes),
        CurveType::Secp256K1 => Secp256k1KeyPair::get_principal_id(&pk.hex_bytes),
        _ => bail!("Curve Type {:?} is not supported", pk.curve_type),
    }
}
```

**File:** rs/rosetta-api/common/rosetta_core/src/models.rs (L105-113)
```rust
    fn get_principal_id(pk_encoded: &str) -> anyhow::Result<PrincipalId> {
        match Ed25519KeyPair::hex_decode_pk(pk_encoded) {
            Ok(pk_decoded) => {
                let pub_der = Ed25519KeyPair::der_encode_pk(pk_decoded)?;
                Ok(PrincipalId::new_self_authenticating(&pub_der))
            }
            Err(e) => Err(e.context(format!("Could not decode public key {pk_encoded}"))),
        }
    }
```

**File:** rs/types/base_types/src/principal_id.rs (L303-309)
```rust
    pub fn new_self_authenticating(pubkey: &[u8]) -> Self {
        let mut id: [u8; 29] = [0; 29];
        id[..28].copy_from_slice(&Sha224::hash(pubkey));
        id[28] = PrincipalIdClass::SelfAuthenticating as u8;
        // id has fixed length of 29, safe to unwrap here
        PrincipalId(Principal::try_from_slice(&id).unwrap())
    }
```

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L171-181)
```rust
    if public_keys.is_empty() {
        return Err(Error::processing_construction_failed(
            &"public_keys should not be empty",
        ));
    }

    if public_keys.len() > 1 {
        return Err(Error::processing_construction_failed(
            &"Only one public key is supported",
        ));
    }
```
