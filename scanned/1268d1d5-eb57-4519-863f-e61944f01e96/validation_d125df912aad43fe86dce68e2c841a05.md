### Title
Unbounded `ingress_expiries` Vec Allocation via Attacker-Controlled `ingress_end`/`ingress_start` in `construction_payloads` — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

---

### Summary

The ICP Rosetta API's `construction_payloads` handler accepts attacker-controlled `ingress_start` and `ingress_end` values from `ConstructionPayloadsRequestMetadata` and uses them directly in an unbounded `while` loop that grows a `Vec<u64>` (`ingress_expiries`) proportional to `(ingress_end - ingress_start) / interval`. There is no cap or validation on the window size. A single small HTTP POST request with a maximally-spread window causes the Rosetta process to allocate tens of gigabytes of heap memory, crashing it via OOM.

---

### Finding Description

In `construction_payloads`, the loop step `interval` is computed as:

```
interval = MAX_INGRESS_TTL − PERMITTED_DRIFT − 120 s
         = 300 s − 60 s − 120 s = 120 s = 120,000,000,000 ns
``` [1](#0-0) 

`ingress_start` and `ingress_end` are read directly from the request metadata with no bounds check: [2](#0-1) 

They are then fed into an unbounded loop: [3](#0-2) 

The server enforces a 4 MB JSON body limit on incoming requests: [4](#0-3) 

This limit is irrelevant to the attack: the malicious request body is tiny (two `u64` values in JSON). The unbounded allocation happens entirely server-side, after parsing.

The `ingress_expiries` Vec is then passed to `add_payloads`, which allocates **two** `SigningPayload` structs (each containing hex-encoded strings) per expiry per transaction: [5](#0-4) 

---

### Impact Explanation

With `ingress_end − ingress_start = u64::MAX / 2 ≈ 9.22 × 10¹⁸ ns`:

| Stage | Calculation | Size |
|---|---|---|
| Loop iterations | 9.22e18 / 1.2e11 | ~76.8 million |
| `ingress_expiries` Vec | 76.8M × 8 bytes | ~614 MB |
| `SigningPayload` objects (2 per expiry, ~200 bytes each) | 76.8M × 2 × 200 B | ~30 GB |

A single request causes the Rosetta process to attempt a ~30 GB heap allocation, triggering OOM and crashing the process. Because Rosetta is a single-process service (not a canister), there is no isolation or restart guarantee at the protocol level. The crash is a complete denial of service for all Rosetta API consumers.

`MAX_INGRESS_TTL` is 5 minutes: [6](#0-5) 

---

### Likelihood Explanation

- No authentication is required to call `POST /construction/payloads`.
- The request body is a standard JSON object; the attacker only needs to set two numeric fields.
- The exploit is deterministic and reproducible with a single HTTP request.
- No rate limiting or per-request memory accounting exists in the Rosetta server for this endpoint.

---

### Recommendation

Add a hard cap on the ingress window before the loop. For example:

```rust
const MAX_INGRESS_WINDOW: Duration = Duration::from_secs(24 * 60 * 60); // 1 day cap
if ingress_end > ingress_start + MAX_INGRESS_WINDOW {
    return Err(ApiError::invalid_request("ingress window exceeds maximum allowed duration"));
}
```

Alternatively, cap the number of iterations directly:

```rust
const MAX_INGRESS_EXPIRIES: usize = 1000;
while now < ingress_end {
    if ingress_expiries.len() >= MAX_INGRESS_EXPIRIES {
        return Err(ApiError::invalid_request("too many ingress expiries requested"));
    }
    // ...
}
```

The ICRC1 Rosetta implementation has partial validation (rejecting `ingress_start >= ingress_end` and stale windows) but also lacks a window-size cap and is similarly affected.

---

### Proof of Concept

```bash
curl -s -X POST http://<rosetta-host>:8080/construction/payloads \
  -H 'Content-Type: application/json' \
  -d '{
    "network_identifier": {"blockchain":"Internet Computer","network":"00000000000000020101"},
    "operations": [{"operation_identifier":{"index":0},"type":"TRANSACTION","account":{"address":"abc"},"amount":{"value":"-1","currency":{"symbol":"ICP","decimals":8}}}],
    "public_keys": [{"hex_bytes":"0000000000000000000000000000000000000000000000000000000000000001","curve_type":"edwards25519"}],
    "metadata": {
      "ingress_start": 0,
      "ingress_end": 9223372036854775807
    }
  }'
```

Expected observable effect: the Rosetta process RSS grows rapidly to available RAM and the process is killed by the OS OOM killer, returning no response. Heap growth can be confirmed with `perf` or `/proc/<pid>/status` polling during the request.

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

**File:** rs/rosetta-api/icp/src/rosetta_server.rs (L297-298)
```rust
                    web::JsonConfig::default()
                        .limit(4 * 1024 * 1024)
```

**File:** rs/limits/src/lib.rs (L17-17)
```rust
pub const MAX_INGRESS_TTL: Duration = Duration::from_secs(5 * 60); // 5 minutes
```
