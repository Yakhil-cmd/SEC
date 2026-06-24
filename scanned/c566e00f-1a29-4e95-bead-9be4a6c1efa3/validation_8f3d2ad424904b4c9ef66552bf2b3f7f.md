The code is fully readable. Let me confirm the exact data flow before rendering a verdict.

**Tracing the two attacker-controlled label values:**

**Label 1 — `endpoint` (URL path):** [1](#0-0) 
The raw URI path is captured before routing, then passed unsanitized to `observe_request_duration` at: [2](#0-1) 

**Label 2 — `token_display_name` (from `network_identifier.network`):** [3](#0-2) 
The value is taken verbatim from the JSON body. If it doesn't match a known canister ID, `get_display_name_from_canister_id` falls back to returning the raw attacker-supplied string: [4](#0-3) 

Both values are then used as Prometheus label values with no sanitization or cardinality cap: [5](#0-4) 

**Key mitigating factor — `endpoint` label in practice:**
The Axum router is configured with a fixed set of routes (e.g., `/block`, `/network/list`, etc.). [6](#0-5) 
Requests to unknown paths like `/block/1`, `/block/2` are routed to a 404 handler, but the middleware captures `req.uri().path()` **before** `inner.call(req)`, so every unique path still creates a new histogram time series. The `status` label is bounded (HTTP status codes), and `method` is bounded (~9 values), but `endpoint` is fully unbounded.

**Key mitigating factor — `token_display_name` label:**
The application-level handlers do validate `network_identifier.network` against a known canister ID (e.g., `verify_network_id` in ICP Rosetta, `get_state_from_network_id` in ICRC-1 Rosetta): [7](#0-6) [8](#0-7) 

**However, this validation happens AFTER the metrics middleware has already recorded the label.** The middleware runs at the Tower layer level, extracts `network_identifier.network` from the raw body, and calls `observe_request_duration` before the handler ever validates the network ID. The handler returning an error does not undo the Prometheus label allocation.

---

### Title
Unbounded Prometheus Label Cardinality via Attacker-Controlled `endpoint` and `token_display_name` Labels Causes OOM — (`rs/rosetta-api/common/rosetta_core/src/metrics.rs`)

### Summary
The `RosettaMetricsMiddleware` Tower layer records two attacker-controlled strings as Prometheus label values — the raw HTTP request path (`endpoint`) and `network_identifier.network` from the JSON body (`token_display_name`) — with no sanitization, allowlist, or cardinality cap. Each unique label combination permanently allocates a new histogram time series (13 metric objects per combination) in the default Prometheus registry. An unprivileged HTTP client can exhaust heap memory by sending requests with unique paths and/or unique `network_identifier.network` values, crashing the Rosetta process.

### Finding Description
In `RosettaMetricsMiddleware::call`:

1. `let path = req.uri().path().to_owned();` — captures the raw URI path, fully attacker-controlled.
2. `extract_canister_id` parses `network_identifier.network` from the JSON body and returns it as a `String` with no validation.
3. `get_display_name_from_canister_id` returns the raw string if no mapping exists.
4. `metrics.observe_request_duration(&path, &method, &status, duration)` calls `ENDPOINTS_METRICS.request_duration.with_label_values(labels).observe(duration)`, which allocates a new `Histogram` object in the global registry for every unique `(token_display_name, endpoint, method, status)` tuple.

The application-level `verify_network_id` / `get_state_from_network_id` validation occurs inside the route handler, **after** the middleware has already committed the label to the registry. A 404 or 400 response from the handler does not reclaim the allocated metric.

### Impact Explanation
Each unique `(token_display_name, endpoint, method, status)` combination allocates a `Histogram` with the default bucket set (~13 `f64` values) plus registry bookkeeping. Sending N requests with unique paths and M requests with unique `network_identifier.network` values creates up to N×M time series. At ~1 KB per histogram, 100K unique combinations consume ~100 MB; at 1M combinations, ~1 GB. The process crashes with OOM. If Rosetta is the sole bridge for ckBTC/ckETH withdrawals, all in-flight withdrawal requests stall until the process is restarted (and the attack can be repeated immediately).

### Likelihood Explanation
The attack requires only the ability to send HTTP POST requests to a publicly reachable Rosetta endpoint — no authentication, no special privileges. A trivial script sending requests with incrementing path suffixes (`/block/1`, `/block/2`, ...) and unique `network_identifier.network` UUIDs is sufficient. The attack is persistent: each request permanently grows the registry until restart.

### Recommendation
1. **Normalize the `endpoint` label** to a fixed allowlist of known route patterns (e.g., `/block`, `/network/status`) before recording. Any unrecognized path should be mapped to a sentinel value like `"unknown"`.
2. **Validate `token_display_name`** against the set of configured canister IDs before using it as a label value. If the value is not in the known set, use a sentinel like `"unknown"` or the default display name.
3. Consider adding a global cardinality cap using a `DashMap` with a maximum size, rejecting new label combinations beyond the cap.

### Proof of Concept
```python
import requests, uuid, threading

TARGET = "http://<rosetta-host>:8080"

def flood(n):
    for i in range(n):
        requests.post(
            f"{TARGET}/block/{i}",  # unique endpoint label per request
            json={"network_identifier": {"blockchain": "Internet Computer",
                                          "network": str(uuid.uuid4())}},  # unique token_display_name
            timeout=2
        )

threads = [threading.Thread(target=flood, args=(10000,)) for _ in range(10)]
for t in threads: t.start()
for t in threads: t.join()
# After 100K requests: Rosetta OOMs or becomes unresponsive
```

### Citations

**File:** rs/rosetta-api/common/rosetta_core/src/metrics.rs (L92-105)
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
        }
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

**File:** rs/rosetta-api/common/rosetta_core/src/metrics.rs (L326-326)
```rust
        let path = req.uri().path().to_owned();
```

**File:** rs/rosetta-api/common/rosetta_core/src/metrics.rs (L369-369)
```rust
                    metrics.observe_request_duration(&path, &method, &status, duration);
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

**File:** rs/rosetta-api/icp/src/request_handler.rs (L939-951)
```rust
fn verify_network_id(canister_id: &CanisterId, net_id: &NetworkIdentifier) -> Result<(), ApiError> {
    verify_network_blockchain(net_id)?;
    let id = CanisterId::try_from(net_id).map_err(|err| {
        let err_msg = format!("Invalid network ID ('{net_id:?}'): {err:?}");
        debug!("{err_msg}");
        ApiError::InvalidNetworkId(false, Details::from(err_msg))
    })?;
    if *canister_id != id {
        let err_msg = format!("Invalid canister ID (expected '{canister_id}', received '{id}')");
        debug!("{err_msg}");
        return Err(ApiError::InvalidNetworkId(false, Details::from(err_msg)));
    }
    Ok(())
```

**File:** rs/rosetta-api/icrc1/src/common/utils/utils.rs (L26-55)
```rust
pub fn get_state_from_network_id(
    network_identifier: &NetworkIdentifier,
    multitoken_state: &MultiTokenAppState,
) -> anyhow::Result<Arc<AppState>> {
    let state = match multitoken_state
        .token_states
        .get(network_identifier.network.as_str())
    {
        Some(state) => state.clone(),
        None => {
            bail!(
                "Network Identifier {} not being tracked",
                network_identifier.blockchain
            );
        }
    };

    let expected = &NetworkIdentifier::new(
        DEFAULT_BLOCKCHAIN.to_owned(),
        state.icrc1_agent.ledger_canister_id.to_string(),
    );

    if network_identifier != expected {
        bail!(
            "Network Identifiers did not match: Expected {:?} | Actual {:?}",
            expected,
            network_identifier
        )
    }
    Ok(state.clone())
```
