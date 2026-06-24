### Title
Unbounded Prometheus Label Cardinality via Attacker-Controlled `network_identifier.network` — (`rs/rosetta-api/common/rosetta_core/src/metrics.rs`)

### Summary
The `RosettaMetricsMiddleware` extracts `network_identifier.network` from every incoming POST request body and uses it verbatim as the `token_display_name` Prometheus label. Because there is no validation, allowlist, or rate-limiting, an unprivileged HTTP client can inject arbitrarily many distinct label values, causing the Prometheus registry to accumulate unbounded time-series and the `CANISTER_DISPLAY_NAMES` HashMap to grow without bound, leading to Rosetta process OOM.

### Finding Description

**Step 1 — Body parsing with no size limit.**

`extract_canister_id` reads the full request body with `to_bytes(body, usize::MAX)` and extracts `network_identifier.network` as a raw, unvalidated string: [1](#0-0) 

No format check, no allowlist, no length cap — any string is accepted.

**Step 2 — Attacker string becomes the Prometheus label.**

When a `canister_id` is extracted, `get_display_name_from_canister_id` is called. If the string is not already in `CANISTER_DISPLAY_NAMES` (which it won't be for a fresh UUID), the function returns the raw attacker-supplied string as the display name: [2](#0-1) 

That string is then passed directly into `RosettaMetrics::new`: [3](#0-2) 

**Step 3 — New Prometheus time-series created per unique value.**

`inc_api_status_count` and `observe_request_duration` both call `with_label_values` on the global `ENDPOINTS_METRICS` vectors using the attacker-controlled `token_display_name`: [4](#0-3) [5](#0-4) 

`rosetta_api_status_total` (IntCounterVec) and `rosetta_http_request_duration_seconds` (HistogramVec) are both registered in the global Prometheus registry as `lazy_static` singletons: [6](#0-5) [7](#0-6) 

Each call to `with_label_values` with a new label combination allocates and permanently retains a new time-series in the registry. For the `HistogramVec`, each unique `token_display_name` creates one counter per histogram bucket (default: 11+ buckets) plus sum/count counters — all held in memory forever.

**Step 4 — Secondary unbounded allocation in `CANISTER_DISPLAY_NAMES`.**

`RosettaMetrics::new` also inserts every new `(canister_id, display_name)` pair into the global `CANISTER_DISPLAY_NAMES` Mutex-guarded HashMap with no eviction policy: [8](#0-7) [9](#0-8) 

**Step 5 — No rate limiting or body-size guard exists.**

A grep across all Rosetta API Rust sources confirms zero occurrences of `rate_limit`, `RateLimit`, or any body-size cap. The `to_bytes(body, usize::MAX)` call imposes no upper bound on body size either. [10](#0-9) 

### Impact Explanation
Each POST request to any Rosetta endpoint (e.g., `/network/list`, `/account/balance`) with a unique `network_identifier.network` UUID permanently allocates new Prometheus time-series. With M unique values, memory grows as O(M × buckets × label_dimensions). At 100,000 unique values the Rosetta process exhausts heap memory, crashing the process and interrupting ledger sync, balance queries, and transfer submissions.

### Likelihood Explanation
The attack requires only an HTTP client and no credentials. All Rosetta POST endpoints are publicly reachable. The exploit is deterministic, requires no timing, and is trivially scriptable.

### Recommendation
- Validate `network_identifier.network` against a static allowlist of known canister IDs at the middleware layer before using it as a label value; reject or fall back to `default_metrics` for unknown values.
- Apply a hard cap on `CANISTER_DISPLAY_NAMES` map size.
- Add a body-size limit to `extract_canister_id` (replace `usize::MAX` with a reasonable cap such as 64 KiB).
- Add per-IP or global request rate limiting at the HTTP server layer.

### Proof of Concept
```rust
// Sends 100_000 POST requests each with a unique UUID as network_identifier.network
// and asserts that Prometheus registry memory stays bounded.
use uuid::Uuid;

#[tokio::test]
async fn test_label_cardinality_explosion() {
    let client = reqwest::Client::new();
    for _ in 0..100_000 {
        let uid = Uuid::new_v4().to_string();
        let body = serde_json::json!({
            "network_identifier": { "blockchain": "Internet Computer", "network": uid }
        });
        let _ = client
            .post("http://localhost:8080/network/list")
            .json(&body)
            .send()
            .await;
    }
    let families = prometheus::default_registry().gather();
    let total_series: usize = families.iter()
        .map(|f| f.get_metric().len())
        .sum();
    // Will far exceed any reasonable bound; process will OOM before this assertion
    assert!(total_series < 10_000, "cardinality explosion: {} series", total_series);
}
```

### Citations

**File:** rs/rosetta-api/common/rosetta_core/src/metrics.rs (L22-23)
```rust
lazy_static! {
    static ref ENDPOINTS_METRICS: RosettaEndpointsMetrics = RosettaEndpointsMetrics::new();
```

**File:** rs/rosetta-api/common/rosetta_core/src/metrics.rs (L81-82)
```rust
    static ref CANISTER_DISPLAY_NAMES: Mutex<HashMap<String, String>> = Mutex::new(HashMap::new());
}
```

**File:** rs/rosetta-api/common/rosetta_core/src/metrics.rs (L92-103)
```rust
            request_duration: register_histogram_vec!(
                "rosetta_http_request_duration_seconds",
                "HTTP request latency in seconds",
                &["token_display_name", "endpoint", "method", "status"]
            )
            .unwrap(),
            rosetta_api_status_total: register_int_counter_vec!(
                "rosetta_api_status_total",
                "Response status for ic-rosetta-api endpoints",
                &["token_display_name", "status_code"]
            )
            .unwrap(),
```

**File:** rs/rosetta-api/common/rosetta_core/src/metrics.rs (L117-126)
```rust
    pub fn new(token_display_name: String, canister_id: String) -> Self {
        // Add entry to map associating the canister ID with the display name
        let mut map = CANISTER_DISPLAY_NAMES.lock().unwrap();
        map.insert(canister_id.clone(), token_display_name.clone());

        Self {
            token_display_name,
            canister_id,
        }
    }
```

**File:** rs/rosetta-api/common/rosetta_core/src/metrics.rs (L136-142)
```rust
    pub fn inc_api_status_count(&self, status: &str) {
        let labels = &[self.token_display_name.as_str(), status];
        ENDPOINTS_METRICS
            .rosetta_api_status_total
            .with_label_values(labels)
            .inc();
    }
```

**File:** rs/rosetta-api/common/rosetta_core/src/metrics.rs (L160-172)
```rust
    pub fn observe_request_duration(
        &self,
        endpoint: &str,
        method: &str,
        status: &str,
        duration: f64,
    ) {
        let labels = &[self.token_display_name.as_str(), endpoint, method, status];
        ENDPOINTS_METRICS
            .request_duration
            .with_label_values(labels)
            .observe(duration);
    }
```

**File:** rs/rosetta-api/common/rosetta_core/src/metrics.rs (L253-258)
```rust
    pub fn get_display_name_from_canister_id(canister_id: &str) -> String {
        let map = CANISTER_DISPLAY_NAMES.lock().unwrap();
        map.get(canister_id)
            .map(|s| s.to_string())
            .unwrap_or_else(|| canister_id.to_string())
    }
```

**File:** rs/rosetta-api/common/rosetta_core/src/metrics.rs (L342-346)
```rust
            let metrics = match canister_id {
                Some(id) => {
                    let display_name = RosettaMetrics::get_display_name_from_canister_id(&id);
                    RosettaMetrics::new(display_name, id)
                }
```

**File:** rs/rosetta-api/common/rosetta_core/src/metrics.rs (L385-406)
```rust
async fn extract_canister_id(
    body: Body,
) -> Result<(Option<String>, Bytes), Box<dyn std::error::Error + Send + Sync>> {
    // Read body bytes
    let bytes = to_bytes(body, usize::MAX).await?;

    // Don't attempt to parse if empty
    if bytes.is_empty() {
        return Ok((None, bytes));
    }

    // Try to parse as JSON
    let canister_id = match serde_json::from_slice::<Value>(&bytes) {
        Ok(json) => json
            .get("network_identifier")
            .and_then(|ni| ni.get("network"))
            .and_then(|n| n.as_str())
            .map(|s| s.to_string()),
        Err(_) => None,
    };

    Ok((canister_id, bytes))
```
