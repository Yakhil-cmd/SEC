Audit Report

## Title
Unbounded ingress-expiry loop in `construction_payloads` allows OOM via attacker-controlled `ingress_start`/`ingress_end` — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

## Summary
The ICP Rosetta `construction_payloads` handler accepts `ingress_start` and `ingress_end` directly from the unauthenticated HTTP request body and iterates over the window with a fixed 120-second step, pushing one `u64` per iteration into `ingress_expiries` with no bound on window size. An attacker supplying `ingress_start=0` and `ingress_end=u64::MAX` causes approximately 153 billion loop iterations, exhausting process memory and crashing the Rosetta node.

## Finding Description
At [1](#0-0)  `interval` is computed as `MAX_INGRESS_TTL(300s) - PERMITTED_DRIFT(60s) - 120s = 120s = 120_000_000_000 ns`.

At [2](#0-1)  both `ingress_start` and `ingress_end` are taken verbatim from caller-supplied metadata with no validation of the window size.

At [3](#0-2)  the loop `while now < ingress_end` has no iteration cap. With `ingress_start=0` and `ingress_end=u64::MAX (18446744073709551615)`, iterations = `u64::MAX / 120_000_000_000 ≈ 1.53 × 10^11`.

Each entry in `ingress_expiries` is subsequently consumed by `add_payloads` at [4](#0-3) , which pushes **two** heap-allocated `SigningPayload` structs (each containing hex-encoded strings) per expiry per transaction type, multiplying memory consumption further.

The ICRC1 sibling at [5](#0-4)  correctly rejects requests where `ingress_start >= ingress_end` and where `ingress_end < now + ingress_interval`, bounding the window to at most one interval. The ICP Rosetta implementation has no equivalent guard.

## Impact Explanation
An unauthenticated attacker can crash the ICP Rosetta process with a single HTTP request, taking the Rosetta node offline and preventing all ICP ledger operations (transfers, neuron management, staking) that depend on it. This matches the allowed High bounty impact: **Application/platform-level DoS, crash** of a significant financial integration component (ICP Rosetta) with concrete user and protocol harm.

## Likelihood Explanation
The `/construction/payloads` endpoint is unauthenticated and publicly reachable. Exploitation requires a single POST request with two integer fields set to extreme values (`ingress_start=0`, `ingress_end=18446744073709551615`). No credentials, special knowledge, or request volume is needed. The attack is trivially repeatable.

## Recommendation
Add a server-side cap on the ingress window before the loop, mirroring the ICRC1 guard:

```rust
if ingress_start >= ingress_end {
    return Err(ApiError::invalid_request("ingress_start must be before ingress_end"));
}
let max_window = interval * 100; // cap at 100 intervals
if ingress_end > ingress_start + max_window.as_nanos() as u64 {
    return Err(ApiError::invalid_request("ingress window too large"));
}
```

Alternatively, cap `ingress_expiries` to a fixed maximum (e.g., 100 entries) and return an error if the window would exceed it.

## Proof of Concept
```
POST /construction/payloads
{
  "network_identifier": { ... },
  "operations": [{ "type": "REMOVE_HOTKEY", ... }],
  "public_keys": [{ ... }],
  "metadata": {
    "ingress_start": 0,
    "ingress_end": 18446744073709551615
  }
}
```
The Rosetta process enters the `while now < ingress_end` loop at line 101 and allocates memory until OOM. A unit test asserting `ingress_expiries.len() <= 100` after calling `construction_payloads` with this input would fail on the current code.

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
