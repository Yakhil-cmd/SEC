### Title
Unbounded `ingress_expiries` Allocation via Attacker-Controlled `ingress_start`/`ingress_end` in ICP Rosetta `construction_payloads` — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

---

### Summary

The ICP Rosetta server's `POST /construction/payloads` handler accepts attacker-controlled `ingress_start` and `ingress_end` nanosecond timestamps from the request metadata and feeds them directly into an unbounded `while` loop that pushes `u64` values into a `Vec`. No validation of the window size exists. Supplying `ingress_start=0` and `ingress_end=u64::MAX` causes ~153 billion loop iterations and an attempt to allocate ~1.2 TB of heap memory, crashing the single Rosetta process via OOM.

---

### Finding Description

The vulnerable loop is at: [1](#0-0) 

```rust
let mut ingress_expiries = vec![];
let mut now = ingress_start;
while now < ingress_end {
    let ingress_expiry = (now + ...).as_nanos_since_unix_epoch();
    ingress_expiries.push(ingress_expiry);
    now += interval;
}
```

The `interval` is computed as: [2](#0-1) 

```
MAX_INGRESS_TTL(300s) − PERMITTED_DRIFT(60s) − 120s = 120 seconds = 120,000,000,000 ns
``` [3](#0-2) 

`ingress_start` and `ingress_end` are taken verbatim from the user-supplied JSON metadata field (typed `Option<u64>` nanoseconds) with no range or delta validation: [4](#0-3) 

With `ingress_start = 0` and `ingress_end = 18446744073709551615` (`u64::MAX`):

- Iterations: `18_446_744_073_709_551_615 / 120_000_000_000 ≈ 153.7 billion`
- Memory pushed: `153.7 × 10⁹ × 8 bytes ≈ 1.23 TB`

The ICRC1 Rosetta implementation, by contrast, performs explicit validation before its equivalent loop: [5](#0-4) 

```rust
if ingress_start >= ingress_end { return Err(...) }
if ingress_end < now + ingress_interval { return Err(...) }
```

No such guard exists in the ICP handler.

---

### Impact Explanation

The ICP Rosetta server is a single-process HTTP service. An OOM crash terminates the entire process, making the Rosetta API unavailable until the operator restarts it. A single unauthenticated HTTP POST request is sufficient to trigger this. The impact is a non-volumetric, single-request process-level DoS.

---

### Likelihood Explanation

The endpoint requires no authentication. The `metadata` field is a free-form JSON object. Any HTTP client that can reach the Rosetta server can send this request. The Rosetta API is typically exposed to exchange integrators and is reachable over the network.

---

### Recommendation

Add a maximum ingress window guard before the loop in the ICP handler, mirroring the ICRC1 pattern and adding an explicit cap:

```rust
let max_window = interval * MAX_INGRESS_EXPIRY_COUNT; // e.g., cap at ~100 entries
if ingress_end <= ingress_start || (ingress_end - ingress_start) > max_window.as_nanos() as u64 {
    return Err(ApiError::invalid_request("ingress window too large or invalid"));
}
```

Alternatively, cap `ingress_expiries` growth inside the loop with an early `break` once a reasonable maximum (e.g., 100–200 entries) is reached.

---

### Proof of Concept

```rust
// Pseudocode unit test
let req = ConstructionPayloadsRequest {
    metadata: Some(json!({
        "ingress_start": 0u64,
        "ingress_end": u64::MAX,
    })),
    operations: vec![/* valid transfer op */],
    public_keys: Some(vec![/* valid pk */]),
    network_identifier: /* valid */,
};
// Calling construction_payloads(req) will spin the while-loop
// ~153 billion times and exhaust process memory.
handler.construction_payloads(req); // OOM crash
``` [6](#0-5)

### Citations

**File:** rs/rosetta-api/icp/src/request_handler/construction_payloads.rs (L43-107)
```rust
    pub fn construction_payloads(
        &self,
        msg: ConstructionPayloadsRequest,
    ) -> Result<ConstructionPayloadsResponse, ApiError> {
        verify_network_id(self.ledger.ledger_canister_id(), &msg.network_identifier)?;

        let ops = msg.operations.clone();

        let pks = msg.public_keys.clone().ok_or_else(|| {
            const NO_PUBLIC_KEYS: &str = "Expected field 'public_keys' to be populated";
            debug!("{NO_PUBLIC_KEYS}");
            ApiError::internal_error(NO_PUBLIC_KEYS)
        })?;
        let transactions =
            convert::operations_to_requests(&ops, false, self.ledger.token_symbol())?;

        let interval =
            ic_limits::MAX_INGRESS_TTL - ic_limits::PERMITTED_DRIFT - Duration::from_secs(120);

        let meta: Option<ConstructionPayloadsRequestMetadata> = msg
            .metadata
            .as_ref()
            .map(|m| ConstructionPayloadsRequestMetadata::try_from(m.clone()))
            .transpose()
            .map_err(|e| {
                let err_msg =
                    format!("Failed to parse construction payloads request metadata: {e:?}");
                debug!("{}", err_msg);
                e
            })?;

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

        let created_at_time: ic_ledger_core::timestamp::TimeStamp = meta
            .as_ref()
            .and_then(|meta| meta.created_at_time)
            .map(ic_ledger_core::timestamp::TimeStamp::from_nanos_since_unix_epoch)
            .unwrap_or_else(|| std::time::SystemTime::now().into());

        // FIXME: the memo field needs to be associated with the operation
        let memo: Memo = meta
            .as_ref()
            .and_then(|meta| meta.memo)
            .map(Memo)
            .unwrap_or_else(|| Memo(rand::thread_rng().r#gen()));

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
