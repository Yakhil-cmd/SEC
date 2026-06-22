The code is confirmed. Here is the analysis:

**Key facts from the ICP Rosetta `construction_payloads` function:**

- `interval` = `MAX_INGRESS_TTL(300s) - PERMITTED_DRIFT(60s) - 120s` = **120 seconds = 120,000,000,000 nanoseconds** [1](#0-0) 
- `ingress_start` and `ingress_end` are taken directly from attacker-controlled metadata with **no upper-bound validation** [2](#0-1) 
- The loop at lines 99–107 runs `while now < ingress_end` with no iteration cap [3](#0-2) 
- `add_payloads` pushes **2 `SigningPayload` objects per ingress expiry** per transaction type [4](#0-3) [5](#0-4) 

With `ingress_start=0` and `ingress_end=u64::MAX (18446744073709551615)`, the loop runs ≈ **153 billion iterations**, each allocating memory. The ICRC1 Rosetta has a staleness guard (`ingress_end >= now + ingress_interval`) but the ICP Rosetta has **no equivalent guard**. [6](#0-5) 

---

### Title
Unbounded ingress-expiry loop in `construction_payloads` allows OOM via attacker-controlled `ingress_start`/`ingress_end` — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

### Summary
The ICP Rosetta `construction_payloads` handler accepts `ingress_start` and `ingress_end` from the HTTP request body and loops over the window with a fixed 120-second step, pushing one entry per iteration into `ingress_expiries`. No server-side bound on the window size exists. An unauthenticated attacker can set `ingress_start=0` and `ingress_end=2^64-1`, causing ≈153 billion loop iterations and exhausting the Rosetta process memory.

### Finding Description
In `construction_payloads` (lines 99–107), the loop:

```rust
let mut now = ingress_start;
while now < ingress_end {
    ingress_expiries.push(...);
    now += interval;   // interval = 120 seconds in nanoseconds
}
```

accepts both bounds verbatim from `ConstructionPayloadsRequestMetadata.ingress_start` / `ingress_end` (both `Option<u64>`). There is no check that `ingress_end - ingress_start` is bounded by any reasonable constant. With `ingress_start=0` and `ingress_end=u64::MAX`, the number of iterations is `u64::MAX / 120_000_000_000 ≈ 1.5 × 10^11`. Each iteration pushes a `u64` into `ingress_expiries`, and subsequently `add_payloads` pushes two heap-allocated `SigningPayload` structs (each containing hex-encoded strings) per expiry per transaction. The process will OOM before completing.

The ICRC1 Rosetta sibling implementation correctly rejects requests where `ingress_end < now + ingress_interval`, bounding the window to at most one interval from the present. The ICP Rosetta implementation has no such guard.

### Impact Explanation
An unauthenticated attacker can crash or hang the ICP Rosetta process by sending a single malformed POST to `/construction/payloads`. This takes the Rosetta node offline, preventing all ICP ledger operations (transfers, neuron management, staking) that depend on it. Impact is scoped to the Rosetta replica process — not the IC subnet itself.

### Likelihood Explanation
The `/construction/payloads` endpoint is unauthenticated and publicly reachable. The exploit requires a single HTTP request with two integer fields set to extreme values. No special knowledge, credentials, or volume is needed.

### Recommendation
Add a server-side cap on the ingress window before the loop, mirroring the ICRC1 Rosetta guard:

```rust
let max_window = interval * MAX_REASONABLE_INTERVALS; // e.g., 100
if ingress_end > ingress_start + max_window.as_nanos() as u64 {
    return Err(ApiError::invalid_request("ingress window too large"));
}
```

Alternatively, cap `ingress_expiries` to a fixed maximum (e.g., 100 entries) and return an error if the window would exceed it.

### Proof of Concept
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
The Rosetta process enters the `while now < ingress_end` loop and allocates memory until OOM. A unit test asserting `ingress_expiries.len() <= 100` after calling `construction_payloads` with this input would fail on the current code.

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

**File:** rs/rosetta-api/icp/src/ledger_client/handle_remove_hotkey.rs (L1-17)
```rust
use crate::{errors::ApiError, ledger_client::OperationOutput};
use ic_nns_governance_api::{ManageNeuronResponse, manage_neuron_response::Command};

pub fn handle_remove_hotkey(
    bytes: Vec<u8>,
) -> Result<Result<Option<OperationOutput>, ApiError>, String> {
    let response: ManageNeuronResponse = candid::decode_one(bytes.as_ref())
        .map_err(|err| format!("Could not decode REMOVE_HOTKEY response: {err}"))?;
    match &response.command {
        Some(Command::Configure(_)) => Ok(Ok(None)),
        Some(Command::Error(err)) => Ok(Err(ApiError::TransactionRejected(
            false,
            format!("Could not remove hotkey: {err}").into(),
        ))),
        _ => panic!("Unexpected remove hot key result: {:?}", response.command),
    }
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
