Now I have all the information needed to make a determination. Let me analyze the key facts:

**The loop in question** (`construction_payloads.rs`, lines 99-107):
- `interval = MAX_INGRESS_TTL - PERMITTED_DRIFT - 120s = 300s - 60s - 120s = 120 seconds`
- `ingress_start` and `ingress_end` are taken directly from attacker-supplied metadata with **no bounds validation**
- With `ingress_start=0` and `ingress_end=u64::MAX` (~1.84×10¹⁹ ns), iterations ≈ 1.84×10¹⁹ / (120×10⁹) ≈ **153 million iterations**

**The ICRC1 Rosetta** (`rs/rosetta-api/icrc1/src/construction_api/services.rs`, lines 148-158) does validate the window, but the **ICP Rosetta** (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`) has **no such guard**.

**Memory impact**: 153M entries × 8 bytes = ~1.2 GB for `ingress_expiries` alone. Then `add_payloads` creates 2 `SigningPayload` objects per expiry per transaction, multiplying the allocation further.

---

### Title
Unbounded ingress-expiry loop in ICP Rosetta `construction_payloads` causes memory exhaustion — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

### Summary
The ICP Rosetta `/construction/payloads` endpoint accepts attacker-controlled `ingress_start` and `ingress_end` metadata fields and loops over them with no upper bound on the number of iterations, allocating O(N) memory proportional to the window size.

### Finding Description
In `construction_payloads`, the loop at lines 99–107 iterates from `ingress_start` to `ingress_end` in steps of `interval` (120 seconds = 1.2×10¹¹ ns), pushing one `u64` per step into `ingress_expiries`:

```rust
// rs/rosetta-api/icp/src/request_handler/construction_payloads.rs:99-107
let mut ingress_expiries = vec![];
let mut now = ingress_start;
while now < ingress_end {
    ingress_expiries.push(...);
    now += interval;
}
``` [1](#0-0) 

Both `ingress_start` and `ingress_end` are deserialized directly from the JSON request body as raw `u64` nanosecond timestamps with no validation of the window size: [2](#0-1) 

The `interval` is fixed at `MAX_INGRESS_TTL - PERMITTED_DRIFT - 120s`: [3](#0-2) 

`MAX_INGRESS_TTL = 300s`, `PERMITTED_DRIFT = 60s`, so `interval = 120s = 1.2×10¹¹ ns`: [4](#0-3) 

With `ingress_start=0` and `ingress_end=18446744073709551615` (u64::MAX), the loop runs ~153 million iterations. The resulting `ingress_expiries` slice is then passed to `add_payloads`, which allocates **2 `SigningPayload` objects per expiry** per transaction: [5](#0-4) 

The ICRC1 Rosetta counterpart correctly validates the window (rejecting `ingress_end < now + ingress_interval`), but the ICP Rosetta has no equivalent guard: [6](#0-5) 

### Impact Explanation
A single unauthenticated HTTP POST to `/construction/payloads` with a maximally wide ingress window causes the Rosetta process to allocate gigabytes of memory (~1.2 GB for `ingress_expiries` alone, multiplied further by `payloads`), leading to OOM termination or extreme latency. All concurrent Rosetta users are denied service. The request body itself is tiny (two JSON integers), so no volumetric bandwidth is required.

### Likelihood Explanation
The `/construction/payloads` endpoint is public and requires no authentication. The metadata fields `ingress_start` and `ingress_end` are documented as optional u64 nanosecond timestamps. Any unprivileged attacker who can reach the Rosetta HTTP port can trigger this with a single request.

### Recommendation
Add a server-side cap on the number of ingress expiries before the loop executes, mirroring the ICRC1 Rosetta's validation pattern. For example:

```rust
let max_expiries = 10usize; // or derive from MAX_INGRESS_TTL / interval
if (ingress_end - ingress_start) / interval.as_nanos() as u64 > max_expiries as u64 {
    return Err(ApiError::invalid_request("ingress window too large"));
}
```

Alternatively, clamp `ingress_end` to `ingress_start + N * interval` for a small constant N (e.g., N=5).

### Proof of Concept
```
POST /construction/payloads HTTP/1.1
Content-Type: application/json

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

The server enters the unbounded loop at line 101, allocates ~153 million u64 entries into `ingress_expiries`, then allocates ~306 million `SigningPayload` structs in `add_payloads`, exhausting process memory. [1](#0-0) [7](#0-6)

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

**File:** rs/rosetta-api/icp/src/models.rs (L207-217)
```rust
    /// The earliest acceptable expiry date for a ledger transfer.
    /// Must be within 24 hours from created_at_time.
    /// Represents number of nanoseconds since UNIX epoch.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ingress_start: Option<u64>,

    /// The latest acceptable expiry date for a ledger transfer.
    /// Must be within 24 hours from created_at_time.
    /// Represents number of nanoseconds since UNIX epoch.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ingress_end: Option<u64>,
```
