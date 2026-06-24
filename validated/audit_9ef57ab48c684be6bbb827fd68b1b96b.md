Audit Report

## Title
Unbounded Ingress Expiry Loop in ICP Rosetta `construction_payloads` Enables Denial-of-Service — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

## Summary
The `construction_payloads` handler in ICP Rosetta accepts client-supplied `ingress_start`/`ingress_end` nanosecond timestamps with no range validation, then loops from `ingress_start` to `ingress_end` in 120-second steps, pushing one entry per step into `ingress_expiries`. A single unauthenticated HTTP POST with a 10-year window causes ~2.63 million loop iterations and ~5.26 million `SigningPayload` allocations, exhausting memory and CPU and crashing the Rosetta node.

## Finding Description
**Interval computation** (`construction_payloads.rs` L59–60):
```
interval = MAX_INGRESS_TTL(300s) - PERMITTED_DRIFT(60s) - 120s = 120s
```

**Unbounded loop** (`construction_payloads.rs` L99–107): The loop runs `(ingress_end - ingress_start) / interval` times with no cap:
```rust
let mut ingress_expiries = vec![];
let mut now = ingress_start;
while now < ingress_end {
    ingress_expiries.push(...);
    now += interval;
}
```

**No validation before the loop** (`construction_payloads.rs` L74–84): `ingress_start` and `ingress_end` are taken directly from the client-supplied `Option<u64>` metadata fields with no range check:
```rust
let ingress_start = meta.as_ref().and_then(|meta| meta.ingress_start)
    .map(ic_types::time::Time::from_nanos_since_unix_epoch)
    .unwrap_or_else(ic_types::time::current_time);
let ingress_end = meta.as_ref().and_then(|meta| meta.ingress_end)
    .map(ic_types::time::Time::from_nanos_since_unix_epoch)
    .unwrap_or_else(|| ingress_start + interval);
```

**Payload amplification** (`construction_payloads.rs` L1048–1076): `add_payloads` is called once per transaction and iterates over all `ingress_expiries`, allocating **two** `SigningPayload` objects per entry (one update payload, one read-state payload), each containing a hex-encoded hash, an `AccountIdentifier`, and a `SignatureType`.

**Unauthenticated synchronous handler** (`rosetta_server.rs` L124–131): The endpoint is a plain unauthenticated `#[post("/construction/payloads")]` that calls `construction_payloads` synchronously, blocking the Actix worker thread for the full duration of the computation.

**Existing body-size limit is insufficient** (`rosetta_server.rs` L297–303): The 4 MB JSON body limit does not prevent this attack — the malicious payload is two `u64` integers, well under 100 bytes.

**ICRC1 has the guard; ICP does not** (`icrc1/src/construction_api/services.rs` L148–158): The ICRC1 implementation explicitly rejects `ingress_start >= ingress_end` and enforces a minimum window, but the ICP implementation has no equivalent check.

## Impact Explanation
This is an **application/platform-level DoS** matching the High ($2,000–$10,000) impact class: "Application/platform-level DoS, crash, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS." A single request with a 10-year window (`ingress_end - ingress_start = 315,576,000s`) causes ~2,629,800 loop iterations and ~5.26 million `SigningPayload` allocations. At 200+ bytes each, this exceeds 1 GB of heap allocation per request. Multiple concurrent requests trivially OOM-kill the Rosetta node, making it unavailable for all users relying on it for ICP transfers and neuron management.

## Likelihood Explanation
No authentication is required. The request body is tiny (two JSON integers). The attack is trivially reproducible with `curl`. The Rosetta node is a production financial integration component. Any unprivileged network client can trigger this. The ICRC1 Rosetta implementation already has the fix, confirming the ICP omission is an oversight rather than intentional design.

## Recommendation
Add a maximum window check immediately after computing `ingress_start` and `ingress_end`, before the loop in `construction_payloads` (`construction_payloads.rs` ~L85):

```rust
let max_window = interval * MAX_INGRESS_EXPIRIES; // e.g., MAX_INGRESS_EXPIRIES = 100
if ingress_end > ingress_start + max_window {
    return Err(ApiError::invalid_request(
        "ingress_end exceeds maximum allowed window"
    ));
}
```

Alternatively, mirror the ICRC1 validation pattern: reject `ingress_start >= ingress_end` and enforce a reasonable upper bound on `ingress_end - ingress_start` (e.g., capped at a few hours, not years).

## Proof of Concept
```bash
curl -X POST http://<rosetta-node>:8080/construction/payloads \
  -H 'Content-Type: application/json' \
  -d '{
    "network_identifier": {"blockchain":"Internet Computer","network":"<ledger_id>"},
    "operations": [{"operation_identifier":{"index":0},"type":"TRANSACTION",
      "account":{"address":"<addr>"},"amount":{"value":"-1",
      "currency":{"symbol":"ICP","decimals":8}}}],
    "public_keys": [{"hex_bytes":"<pubkey>","curve_type":"edwards25519"}],
    "metadata": {
      "ingress_start": 1000000000000000000,
      "ingress_end":   1315576000000000000
    }
  }'
```

`ingress_end - ingress_start` = 315,576,000,000,000,000 ns = ~10 years. With `interval = 120s`, the loop executes ~2,629,800 times, allocating ~5.26 million `SigningPayload` objects before the process runs out of memory. A unit test reproducing this can be added to `rs/rosetta-api/icp/tests/` by calling `construction_payloads` directly with these metadata values and asserting it returns an error rather than hanging.