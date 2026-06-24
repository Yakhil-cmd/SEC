Looking at the full code, I need to trace both attack vectors precisely.

The code is fully readable. I have everything needed for a complete analysis.

**Two concrete, unsanitized label vectors exist:**

**Vector 1 — `endpoint` label (URI path):** [1](#0-0) 
The raw URI path is captured with no normalization or allowlist check, then passed directly into: [2](#0-1) 
which calls `with_label_values` with the raw path as the `endpoint` label: [3](#0-2) 

**Vector 2 — `token_display_name` label (`network_identifier.network`):**
The body is parsed and `network_identifier.network` is extracted as `canister_id`: [4](#0-3) 
`get_display_name_from_canister_id` falls back to returning the raw attacker-controlled string when no mapping exists: [5](#0-4) 
This value is then used as the `token_display_name` Prometheus label — also unsanitized and unbounded.

**The middleware wraps the entire Axum router** (including unmatched routes that return 404), so every request — regardless of whether it matches a defined route — goes through the metrics recording path: [6](#0-5) 

**Secondary unbounded growth:** `CANISTER_DISPLAY_NAMES` HashMap also grows without bound as `RosettaMetrics::new` inserts every attacker-supplied canister ID: [7](#0-6) 

---

### Title
Unbounded Prometheus Label Cardinality via Attacker-Controlled URI Path and `network_identifier.network` Causes Memory Exhaustion in Rosetta Process — (`rs/rosetta-api/common/rosetta_core/src/metrics.rs`)

### Summary
`RosettaMetricsMiddleware::call` uses the raw HTTP request URI path and the `network_identifier.network` JSON body field as Prometheus label values without any sanitization, allowlisting, or cardinality cap. An unprivileged HTTP client can exhaust the Rosetta process memory by sending requests with unboundedly many unique paths and/or unique `network_identifier.network` values, each creating a new time series in the global Prometheus registry.

### Finding Description
In `RosettaMetricsMiddleware::call`:

1. **Path label**: `let path = req.uri().path().to_owned()` (line 326) captures the raw URI path. This is passed to `observe_request_duration(&path, ...)` (line 369), which calls `ENDPOINTS_METRICS.request_duration.with_label_values([token, path, method, status]).observe(duration)` (lines 167–171). Every unique path string creates a new `HistogramVec` time series (with 15+ buckets each) in the global Prometheus registry.

2. **`token_display_name` label**: `extract_canister_id` reads `network_identifier.network` from the JSON body (lines 397–402) and returns it as the `canister_id`. `get_display_name_from_canister_id` returns the raw string when no pre-registered mapping exists (lines 253–258). `RosettaMetrics::new` also inserts every such value into the `CANISTER_DISPLAY_NAMES` `HashMap` (lines 119–120), which itself grows without bound.

3. **Middleware scope**: The `metrics_layer` is applied to the entire Axum `Router` (line 383 of `main.rs`), meaning every HTTP request — including those to unregistered paths that return 404 — passes through the metrics recording code.

The `method` and `status` labels are bounded (finite HTTP methods and status codes), but `endpoint` and `token_display_name` are both fully attacker-controlled and unbounded.

### Impact Explanation
Each unique `(token_display_name, endpoint, method, status)` combination allocates a new `Histogram` with its full bucket array in the global Prometheus registry. Sending N unique paths creates O(N × buckets) allocations. At 10,000 unique paths with the default 11 histogram buckets, this creates ~110,000 live histogram objects. At scale this exhausts process heap memory, causing the Rosetta process to crash (OOM kill or panic on allocation failure), making the Rosetta API unavailable to all legitimate clients (exchanges, wallets).

### Likelihood Explanation
The Rosetta HTTP port is publicly accessible by design (bound to `0.0.0.0`, line 393 of `main.rs`). No authentication is required. The attack requires only the ability to send HTTP POST requests with arbitrary paths and JSON bodies — no credentials, no privileged access. The attack is trivially scriptable and requires no special knowledge beyond the Rosetta API's public interface.

### Recommendation
- **Normalize the `endpoint` label** to the matched Axum route template (e.g., use `axum::extract::MatchedPath` which returns `/block` rather than the raw URI). For unmatched routes, use a fixed label value such as `"unknown"` or `"404"`.
- **Validate `network_identifier.network`** against a pre-registered allowlist of known canister IDs before using it as a label value. If the value is not in the allowlist, use the default `token_display_name` rather than the raw attacker-supplied string.
- **Remove the `CANISTER_DISPLAY_NAMES` insertion** from the per-request hot path (`RosettaMetrics::new`); populate it only at startup from trusted configuration.

### Proof of Concept
```python
import requests, threading

TARGET = "http://<rosetta-host>:8080"

def flood(n):
    for i in range(n):
        requests.post(
            f"{TARGET}/unique_path_{i}",
            json={"network_identifier": {"network": f"fake-canister-{i}"}},
            timeout=2
        )

threads = [threading.Thread(target=flood, args=(2500,)) for _ in range(4)]
for t in threads: t.start()
for t in threads: t.join()
# After 10000 unique (path, canister_id) pairs, monitor Rosetta RSS:
# watch -n1 'ps aux | grep rosetta'
# Expect OOM crash or unbounded memory growth in the Prometheus registry.
```

### Citations

**File:** rs/rosetta-api/common/rosetta_core/src/metrics.rs (L119-120)
```rust
        let mut map = CANISTER_DISPLAY_NAMES.lock().unwrap();
        map.insert(canister_id.clone(), token_display_name.clone());
```

**File:** rs/rosetta-api/common/rosetta_core/src/metrics.rs (L167-171)
```rust
        let labels = &[self.token_display_name.as_str(), endpoint, method, status];
        ENDPOINTS_METRICS
            .request_duration
            .with_label_values(labels)
            .observe(duration);
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

**File:** rs/rosetta-api/common/rosetta_core/src/metrics.rs (L326-326)
```rust
        let path = req.uri().path().to_owned();
```

**File:** rs/rosetta-api/common/rosetta_core/src/metrics.rs (L369-369)
```rust
                    metrics.observe_request_duration(&path, &method, &status, duration);
```

**File:** rs/rosetta-api/common/rosetta_core/src/metrics.rs (L397-402)
```rust
    let canister_id = match serde_json::from_slice::<Value>(&bytes) {
        Ok(json) => json
            .get("network_identifier")
            .and_then(|ni| ni.get("network"))
            .and_then(|n| n.as_str())
            .map(|s| s.to_string()),
```

**File:** rs/rosetta-api/icrc1/src/main.rs (L361-383)
```rust
    let app = Router::new()
        .route("/ready", get(ready))
        .route("/health", get(health))
        .route("/call", post(call))
        .route("/network/list", post(network_list))
        .route("/network/options", post(network_options))
        .route("/network/status", post(network_status))
        .route("/block", post(block))
        .route("/account/balance", post(account_balance))
        .route("/block/transaction", post(block_transaction))
        .route("/search/transactions", post(search_transactions))
        .route("/mempool", post(mempool))
        .route("/mempool/transaction", post(mempool_transaction))
        .route("/construction/derive", post(construction_derive))
        .route("/construction/preprocess", post(construction_preprocess))
        .route("/construction/metadata", post(construction_metadata))
        .route("/construction/combine", post(construction_combine))
        .route("/construction/submit", post(construction_submit))
        .route("/construction/hash", post(construction_hash))
        .route("/construction/payloads", post(construction_payloads))
        .route("/construction/parse", post(construction_parse))
        // Apply the metrics middleware
        .layer(metrics_layer)
```
