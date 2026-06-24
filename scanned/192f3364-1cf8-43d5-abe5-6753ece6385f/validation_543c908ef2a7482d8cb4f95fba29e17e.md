### Title
Prometheus Label Cardinality Explosion via Unsanitized HTTP Path and Request Body Input — (`rs/rosetta-api/common/rosetta_core/src/metrics.rs`)

### Summary

The `RosettaMetricsMiddleware` uses two attacker-controlled values — the raw HTTP request path and the `network_identifier.network` field from the JSON body — as Prometheus label values with no sanitization, normalization, or cardinality cap. An unprivileged HTTP client can exhaust heap memory by sending requests with unique paths and unique network identifiers, causing the `rosetta_http_request_duration_seconds` HistogramVec and `rosetta_api_status_total` IntCounterVec to allocate O(N) metric objects in the default Prometheus registry.

---

### Finding Description

**Vector 1 — `endpoint` label (raw HTTP path):**

In `RosettaMetricsMiddleware::call`, the raw URI path is captured before the request is dispatched: [1](#0-0) 

It is then passed verbatim as the `endpoint` label to `observe_request_duration`: [2](#0-1) 

The Axum router in `main.rs` defines a fixed set of routes (`/block`, `/account/balance`, etc.): [3](#0-2) 

However, the metrics layer wraps the entire router. For any unregistered path (e.g., `/block/1`, `/block/2`, …), Axum returns an `Ok(404)` response — and the middleware still records the unique path as a label value, because the `match &result { Ok(response) => ... }` branch fires unconditionally for all HTTP responses including 404s. [4](#0-3) 

**Vector 2 — `token_display_name` label (attacker-controlled body field):**

`extract_canister_id` reads `network_identifier.network` from the JSON body: [5](#0-4) 

The extracted value is looked up in `CANISTER_DISPLAY_NAMES`. If not found (i.e., any value not registered at startup), the raw attacker-supplied string is used as the `token_display_name` label: [6](#0-5) 

**Combined effect:**

`rosetta_http_request_duration_seconds` has four labels: `[token_display_name, endpoint, method, status]`. [7](#0-6) 

Each unique `(token_display_name, endpoint, method, status)` tuple causes Prometheus to allocate a new histogram object with ~15 bucket counters. With N unique paths × M unique network identifiers, the registry grows to O(N×M) histogram objects. There is no cardinality cap, no label allowlist, and no rate limiting in the middleware.

---

### Impact Explanation

Unbounded heap growth leads to OOM termination of the Rosetta process. If the Rosetta instance is the sole operational bridge for ckBTC/ckETH withdrawals, all in-flight withdrawal operations stall until the process is restarted. The attack requires no authentication and no privileged access — any HTTP client reachable to the Rosetta port can trigger it.

---

### Likelihood Explanation

The Rosetta API is designed to be publicly reachable. The attack requires only standard HTTP POST requests with crafted JSON bodies and unique URL paths. No exploit tooling beyond `curl` or a simple script is needed. The cardinality explosion is persistent across the process lifetime (Prometheus does not evict label combinations), so even a moderate request rate (e.g., 10K requests/minute) can exhaust memory within minutes.

---

### Recommendation

1. **Normalize the `endpoint` label** to a fixed allowlist of known route strings (e.g., map any path to its matched route template, or drop unknown paths from metrics entirely).
2. **Validate `token_display_name`** against the set of registered canister IDs/display names at startup; reject or replace unknown values with a sentinel like `"unknown"`.
3. **Add a cardinality guard** — reject `with_label_values` calls when the number of existing label combinations exceeds a configured threshold.

---

### Proof of Concept

```python
import requests, threading

BASE = "http://<rosetta-host>:<port>"

def flood(i):
    # Unique path → unique `endpoint` label
    # Unique network value → unique `token_display_name` label
    requests.post(
        f"{BASE}/block/{i}",
        json={"network_identifier": {"network": f"fake-canister-{i}"},
              "block_identifier": {"index": 0}},
        timeout=5
    )

threads = [threading.Thread(target=flood, args=(i,)) for i in range(100_000)]
for t in threads: t.start()
for t in threads: t.join()
```

After this, `GET /metrics` will return a `rosetta_http_request_duration_seconds` family with ~100K distinct label sets (each with ~15 histogram buckets), and the process RSS will have grown proportionally. Repeating the flood will eventually trigger OOM.

### Citations

**File:** rs/rosetta-api/common/rosetta_core/src/metrics.rs (L92-97)
```rust
            request_duration: register_histogram_vec!(
                "rosetta_http_request_duration_seconds",
                "HTTP request latency in seconds",
                &["token_display_name", "endpoint", "method", "status"]
            )
            .unwrap(),
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

**File:** rs/rosetta-api/common/rosetta_core/src/metrics.rs (L363-376)
```rust
            match &result {
                Ok(response) => {
                    let status = response.status().as_u16().to_string();
                    metrics.inc_api_status_count(&status);

                    // Record request duration with status
                    metrics.observe_request_duration(&path, &method, &status, duration);
                }
                Err(_) => {
                    metrics.inc_api_status_count("error");

                    // Record request duration with error status
                    metrics.observe_request_duration(&path, &method, "error", duration);
                }
```

**File:** rs/rosetta-api/common/rosetta_core/src/metrics.rs (L397-403)
```rust
    let canister_id = match serde_json::from_slice::<Value>(&bytes) {
        Ok(json) => json
            .get("network_identifier")
            .and_then(|ni| ni.get("network"))
            .and_then(|n| n.as_str())
            .map(|s| s.to_string()),
        Err(_) => None,
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
