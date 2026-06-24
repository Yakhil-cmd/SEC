### Title
Unbounded `ingress_expiries` Vec Allocation via Attacker-Controlled `ingress_start`/`ingress_end` in ICP Rosetta `construction_payloads` — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

---

### Summary

The ICP Rosetta API's `construction_payloads` handler accepts attacker-controlled `ingress_start` and `ingress_end` nanosecond timestamps from the request metadata and feeds them directly into an unbounded `while` loop with no window-size cap. An unauthenticated client can set `ingress_end - ingress_start` to an arbitrarily large value, causing the server to allocate a `Vec` with tens of millions of entries before any business logic runs, exhausting heap memory and crashing the single Rosetta process.

---

### Finding Description

In `construction_payloads`, the step size (`interval`) is computed as:

```
interval = MAX_INGRESS_TTL − PERMITTED_DRIFT − 120s
         = 300s − 60s − 120s = 120s = 1.2 × 10¹¹ ns
``` [1](#0-0) 

`ingress_start` and `ingress_end` are taken verbatim from the JSON metadata with no validation: [2](#0-1) 

The loop then runs `(ingress_end − ingress_start) / interval` iterations, pushing one `u64` per iteration: [3](#0-2) 

With `ingress_end − ingress_start = u64::MAX / 2 ≈ 9.2 × 10¹⁸ ns`:

```
iterations ≈ 9.2 × 10¹⁸ / 1.2 × 10¹¹ ≈ 77,000,000
```

`ingress_expiries` alone consumes ~616 MB. Then `add_payloads` iterates over every entry and pushes **two** `SigningPayload` structs (each containing heap-allocated `String` fields) per expiry per transaction: [4](#0-3) 

For a single-transaction request this yields ~77M × 2 `SigningPayload` objects, each ~200+ bytes, totalling tens of gigabytes — a guaranteed OOM.

**The ICRC1 Rosetta counterpart has an explicit guard that the ICP version lacks entirely:** [5](#0-4) 

`MAX_INGRESS_TTL` is 5 minutes and `PERMITTED_DRIFT` is 60 seconds: [6](#0-5) 

---

### Impact Explanation

The ICP Rosetta API runs as a single OS process. There is no per-request memory quota, no request timeout on the allocation loop, and no replica redundancy. A single malformed POST to `/construction/payloads` with a large ingress window causes the process to OOM-crash, taking down the entire Rosetta service until it is manually restarted. All in-flight signing sessions are lost.

---

### Likelihood Explanation

The endpoint requires no authentication. The attacker only needs to craft a valid JSON body with two large integer fields (`ingress_start`, `ingress_end`). The attack is reproducible in a local test environment with a single HTTP request and requires no privileged access, no key material, and no coordination.

---

### Recommendation

Add a window-size cap immediately after parsing `ingress_start`/`ingress_end`, mirroring the guard already present in the ICRC1 Rosetta implementation. For example:

```rust
let max_window = interval * MAX_REASONABLE_INTERVALS; // e.g., 24 hours
if ingress_end > ingress_start + max_window {
    return Err(ApiError::invalid_request(
        "ingress_end − ingress_start exceeds the maximum allowed window",
    ));
}
```

Alternatively, cap `ingress_expiries` to a fixed maximum length (e.g., 1440 entries for a 48-hour window at 2-minute intervals) and return an error if the computed count exceeds it.

---

### Proof of Concept

```bash
curl -X POST http://<rosetta-host>:8080/construction/payloads \
  -H 'Content-Type: application/json' \
  -d '{
    "network_identifier": {"blockchain":"Internet Computer","network":"00000000000000020101"},
    "operations": [{"operation_identifier":{"index":0},"type":"STAKE",
      "account":{"address":"<valid-account>"},
      "metadata":{"neuron_index":0}}],
    "public_keys": [{"hex_bytes":"<valid-pubkey>","curve_type":"edwards25519"}],
    "metadata": {
      "ingress_start": 1000000000,
      "ingress_end":   9223372036854775807
    }
  }'
# Expected: server process OOMs and crashes before returning a response.
# Observed heap growth: ~77M u64 entries (~616 MB) in ingress_expiries,
# followed by ~154M SigningPayload allocations (tens of GB) in add_payloads.
```

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
