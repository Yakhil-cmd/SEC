### Title
Unbounded `ingress_expiries` Loop Allows OOM DoS via Crafted `/construction/payloads` Metadata — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

---

### Summary

The ICP Rosetta API's `construction_payloads` handler accepts user-controlled `ingress_start` and `ingress_end` metadata fields as raw `u64` nanosecond values and uses them directly in an unbounded `while` loop with no range validation. An unauthenticated attacker can send a single HTTP POST with `ingress_start=0` and `ingress_end=u64::MAX`, causing the loop to iterate ~154 million times, allocating over 1 GB of memory and crashing the Rosetta process.

---

### Finding Description

In `construction_payloads`, the `interval` step is computed as:

```
MAX_INGRESS_TTL(300s) - PERMITTED_DRIFT(60s) - 120s = 120s = 120,000,000,000 ns
``` [1](#0-0) [2](#0-1) 

`ingress_start` and `ingress_end` are taken directly from user-supplied metadata with no bounds check: [3](#0-2) 

The loop then runs without any guard: [4](#0-3) 

With `ingress_start=0` and `ingress_end=18446744073709551615` (`u64::MAX`):

- **Iterations**: `u64::MAX / 120_000_000_000 ≈ 154,000,000`
- **`ingress_expiries` Vec**: 154M × 8 bytes ≈ **1.23 GB**
- **`payloads` Vec** (via `add_payloads`): 2 `SigningPayload` structs per expiry per transaction, each containing a hex-encoded hash — multiplies memory further [5](#0-4) 

The endpoint is publicly accessible with no authentication: [6](#0-5) 

The HTTP server's 4 MB JSON body limit does not help — the malicious request body is tiny (two JSON integers): [7](#0-6) 

**The ICRC1 Rosetta counterpart already has the correct fix** — it validates that `ingress_start < ingress_end` and that `ingress_end` is within a reasonable future window before entering the loop: [8](#0-7) 

The ICP Rosetta handler has no equivalent guard.

---

### Impact Explanation

A single unauthenticated HTTP POST to `/construction/payloads` with crafted metadata causes the Rosetta process to exhaust available memory and crash (OOM). This denies service to all users of the ICP Rosetta API — including exchanges and custodians that rely on it for ICP ledger transfers and neuron management. The process does not recover without a restart.

---

### Likelihood Explanation

The endpoint requires no credentials. The request body is trivially small. The attack is reproducible locally in seconds. The contrast with the already-fixed ICRC1 version confirms the root cause is a missing validation that was known to be necessary.

---

### Recommendation

Apply the same validation already present in the ICRC1 version before the loop:

1. Reject if `ingress_start >= ingress_end`.
2. Reject if `ingress_end - ingress_start` exceeds a reasonable maximum (e.g., 24 hours = `86_400_000_000_000` ns, yielding at most 720 iterations).
3. Alternatively, cap `ingress_expiries` to a hard maximum count (e.g., 1000) and return an error if the range would exceed it.

---

### Proof of Concept

```http
POST /construction/payloads HTTP/1.1
Content-Type: application/json

{
  "network_identifier": {"blockchain":"Internet Computer","network":"00000000000000020101"},
  "operations": [{"operation_identifier":{"index":0},"type":"TRANSACTION","account":{"address":"..."},"amount":{"value":"-100","currency":{"symbol":"ICP","decimals":8}}}],
  "public_keys": [{"hex_bytes":"...","curve_type":"edwards25519"}],
  "metadata": {
    "ingress_start": 0,
    "ingress_end": 18446744073709551615
  }
}
```

Expected: Rosetta process allocates ~1.2 GB for `ingress_expiries` alone, then OOMs.

Fuzz assertion: `assert!(response.payloads.len() <= MAX_EXPECTED_PAYLOADS)` and `assert!(elapsed < Duration::from_secs(1))` will both fail for any `ingress_end - ingress_start > ~1000 * interval`.

### Citations

**File:** rs/rosetta-api/icp/src/request_handler/construction_payloads.rs (L59-60)
```rust
        let interval =
            ic_limits::MAX_INGRESS_TTL - ic_limits::PERMITTED_DRIFT - Duration::from_secs(120);
```

**File:** rs/rosetta-api/icp/src/request_handler/construction_payloads.rs (L74-84)
```rust
        let ingress_start = meta
            .as_ref()
            .and_then(|meta| meta.ingress_start)
            .map(ic_types::time::Time::from_nanos_since_unix_epoch)
            .unwrap_or_else(ic_types::time::current_time);

        let ingress_end = meta
            .as_ref()
            .and_then(|meta| meta.ingress_end)
            .map(ic_types::time::Time::from_nanos_since_unix_epoch)
            .unwrap_or_else(|| ingress_start + interval);
```

**File:** rs/rosetta-api/icp/src/request_handler/construction_payloads.rs (L99-107)
```rust
        let mut ingress_expiries = vec![];
        let mut now = ingress_start;
        while now < ingress_end {
            let ingress_expiry = (now
                + ic_limits::MAX_INGRESS_TTL.saturating_sub(ic_limits::PERMITTED_DRIFT))
            .as_nanos_since_unix_epoch();
            ingress_expiries.push(ingress_expiry);
            now += interval;
        }
```

**File:** rs/rosetta-api/icp/src/request_handler/construction_payloads.rs (L1046-1075)
```rust
/// Add transaction and read state messages for a given update to the payloads vector.
/// Payloads are added for each ingress expiries.
fn add_payloads(
    payloads: &mut Vec<SigningPayload>,
    ingress_expiries: &[u64],
    account_identifier: &AccountIdentifier,
    update: &HttpCanisterUpdate,
    signature_type: SignatureType,
) {
    for ingress_expiry in ingress_expiries {
        let mut update = update.clone();
        update.ingress_expiry = *ingress_expiry;
        let message_id = update.id();
        let transaction_payload = SigningPayload {
            address: None,
            account_identifier: Some(account_identifier.clone()),
            hex_bytes: hex::encode(make_sig_data(&message_id)),
            signature_type: Some(signature_type),
        };
        payloads.push(transaction_payload);
        let read_state = make_read_state_from_update(&update);
        let read_state_message_id = MessageId::from(read_state.representation_independent_hash());
        let read_state_payload = SigningPayload {
            address: None,
            account_identifier: Some(account_identifier.clone()),
            hex_bytes: hex::encode(make_sig_data(&read_state_message_id)),
            signature_type: Some(signature_type),
        };
        payloads.push(read_state_payload);
    }
```

**File:** rs/limits/src/lib.rs (L17-21)
```rust
pub const MAX_INGRESS_TTL: Duration = Duration::from_secs(5 * 60); // 5 minutes

/// Duration subtracted from `MAX_INGRESS_TTL` by
/// `expiry_time_from_now()` when creating an ingress message.
pub const PERMITTED_DRIFT: Duration = Duration::from_secs(60);
```

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

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L148-158)
```rust
    if ingress_start >= ingress_end {
        return Err(Error::processing_construction_failed(&format!(
            "Ingress start should start before ingress end: Start: {ingress_start}, End: {ingress_end}"
        )));
    }

    if ingress_end < now + ingress_interval {
        return Err(Error::processing_construction_failed(&format!(
            "Ingress end should be at least one interval from the current time: Current time: {now}, End: {ingress_end}"
        )));
    }
```
