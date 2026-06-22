Now I have enough information to make a definitive determination. Let me trace the complete flow.

**Key facts established:**

1. **`validate_subnet_request`** parses the URL path `{subnet_id}` as a `PrincipalId` and inserts it into request extensions — format-only validation, no existence check. [1](#0-0) 

2. **Middleware stack order** in `subnet_layers`: `validate_subnet_request` → `common_layers` (contains `metrics_middleware`) → `subnet_lookup`. The metrics middleware runs *before* `subnet_lookup`. [2](#0-1) 

3. **`metrics_middleware`** reads `subnet_id` from request extensions (set by `validate_subnet_request`) *before* calling `next.run()`. The attacker-controlled value is captured at this point. [3](#0-2) 

4. After `next.run()` returns (which includes `subnet_lookup` failing with `SubnetNotFound`), metrics are unconditionally recorded with the attacker-controlled `subnet_id_lbl`. [4](#0-3) 

5. **`remove_stale_metrics`** operates only on the `Vec<MetricFamily>` returned by `registry.gather()` — it filters what is *exported*, but does **not** call `remove_label_values` on the underlying `HistogramVec`/`IntCounterVec`. The Prometheus registry itself retains all label combinations in memory. [5](#0-4) 

---

### Title
Unbounded Prometheus Label Cardinality via Attacker-Controlled `subnet_id` in Subnet API Routes — (`rs/boundary_node/ic_boundary/src/metrics.rs`)

### Summary
An unprivileged attacker can send HTTP requests to `/api/v2/subnet/{subnet_id}/read_state` (or v3/v4 equivalents) with an unbounded number of syntactically valid but non-existent `subnet_id` Principal values. Each unique value is recorded as a distinct Prometheus label combination in the in-process registry, causing unbounded memory growth and eventual OOM of the boundary node process.

### Finding Description
The `validate_subnet_request` middleware parses the URL path `{subnet_id}` as a `PrincipalId` (format-only check) and inserts it into request extensions. [6](#0-5) 

The `metrics_middleware` (part of `common_layers`) sits *outside* `subnet_lookup` in the middleware stack. It reads the attacker-supplied `SubnetId` from request extensions before dispatching to inner layers: [7](#0-6) 

After `next.run()` returns — even with `ErrorCause::SubnetNotFound` — the middleware unconditionally calls `with_label_values(labels)` on all four `HistogramVec`/`IntCounterVec` metrics, creating a new in-memory time series for each novel `subnet_id` string: [8](#0-7) 

The `remove_stale_metrics` function only filters the `Vec<MetricFamily>` snapshot used for Prometheus export. It does not call `remove_label_values` on the underlying metric objects, so the registry's internal hash maps grow without bound: [9](#0-8) 

### Impact Explanation
Each new `subnet_id` label value creates 4 new metric series (counter, durationer, request_sizer, response_sizer). Each `HistogramVec` series allocates bucket arrays (8 buckets defined). With 4 metrics × ~1 KB per series, 1 million unique subnet IDs ≈ ~4 GB of registry memory. The boundary node process will OOM and crash, causing a denial of service for all traffic routed through that node.

### Likelihood Explanation
The attack requires only valid-format Principal IDs (29-byte values with correct checksum), which are trivially generated. The per-IP rate limit (`rate_limit_per_second_per_ip`) slows the attack but does not prevent it — even at 10 req/s, an attacker accumulates 864,000 unique label combinations per day. The per-subnet rate limiter does not help because each request uses a *different* subnet ID. No authentication is required.

### Recommendation
1. **Validate subnet existence before recording metrics**: Move `subnet_lookup` outside (above) `metrics_middleware` in the layer stack, or record `subnet_id` only from the `Arc<Subnet>` response extension (which is only set on successful lookup).
2. **Alternatively**: In `metrics_middleware`, replace the raw `subnet_id_str` with `SUBNET_ID_UNKNOWN` when the response contains `ErrorCause::SubnetNotFound`, preventing unknown IDs from ever becoming label values.
3. **Defense-in-depth**: Call `remove_label_values` on the underlying metric objects (not just filter the export snapshot) when stale subnet IDs are detected.

### Proof of Concept
```python
import hashlib, struct, base64, requests, threading

def make_principal(n: int) -> str:
    # Construct a syntactically valid but non-existent subnet Principal
    raw = b'\x04' + n.to_bytes(28, 'big')  # type byte + padding
    crc = struct.pack('>I', crc32(raw))
    encoded = base64.b32encode(crc + raw).decode().lower().rstrip('=')
    return '-'.join(encoded[i:i+5] for i in range(0, len(encoded), 5))

for i in range(1_000_000):
    sid = make_principal(i)
    requests.post(f"https://<boundary-node>/api/v2/subnet/{sid}/read_state",
                  data=b'\xa0', headers={"Content-Type": "application/cbor"})
# Monitor boundary node RSS: assert it grows proportionally to i
```

After N iterations, scrape `/metrics` and observe that `http_request_total` cardinality is bounded by known subnets — but the process RSS grows proportionally to N, confirming the registry leak.

### Citations

**File:** rs/boundary_node/ic_boundary/src/http/middleware/validate.rs (L81-89)
```rust
    // Decode subnet ID from URL
    let principal_id: PrincipalId = Principal::from_text(subnet_id.as_str())
        .map_err(|err| {
            ErrorCause::MalformedRequest(format!("Unable to decode subnet_id from URL: {err}"))
        })?
        .into();
    let subnet_id = SubnetId::from(principal_id);

    request.extensions_mut().insert(subnet_id);
```

**File:** rs/boundary_node/ic_boundary/src/core.rs (L1033-1040)
```rust
    let subnet_layers = ServiceBuilder::new()
        .layer(middleware::from_fn(validate::validate_request))
        .layer(middleware::from_fn(validate::validate_subnet_request))
        .layer(common_layers)
        .layer(middleware_subnet_read_state_cache)
        .layer(middleware_subnet_lookup)
        .layer(middleware_generic_limiter)
        .layer(middleware_retry);
```

**File:** rs/boundary_node/ic_boundary/src/metrics.rs (L81-134)
```rust
fn remove_stale_metrics(
    snapshot: Arc<RegistrySnapshot>,
    mut mfs: Vec<MetricFamily>,
) -> Vec<MetricFamily> {
    mfs.iter_mut().for_each(|mf| {
        // Iterate over the metrics in the metric family
        let metrics = mf
            .take_metric()
            .into_iter()
            .filter(|v| {
                // See if this metric has node_id/subnet_id labels
                let node_id = v
                    .get_label()
                    .iter()
                    .find(|&v| v.name() == NODE_ID_LABEL)
                    .map(|x| x.value());

                let subnet_id = v
                    .get_label()
                    .iter()
                    .find(|&v| v.name() == SUBNET_ID_LABEL)
                    .map(|x| x.value());

                match (node_id, subnet_id) {
                    // Check if we got both node_id and subnet_id labels
                    (Some(node_id), Some(subnet_id)) => snapshot
                        .nodes
                        // Check if the node_id is in the snapshot
                        .get(node_id)
                        // Check if its subnet_id matches, otherwise the metric needs to be removed
                        .map(|x| x.subnet_id.to_string() == subnet_id)
                        .unwrap_or(false),

                    // If there's only subnet_id label - check if this subnet exists.
                    // TODO create a hashmap of subnets in snapshot for faster lookup, currently complexity is O(n)
                    // but since we have very few subnets currently (<40) probably it's Ok
                    (None, Some(subnet_id)) => {
                        subnet_id == SUBNET_ID_UNKNOWN
                            || snapshot
                                .subnets
                                .iter()
                                .any(|x| x.id.to_string() == subnet_id)
                    }

                    // Otherwise just pass this metric through
                    _ => true,
                }
            })
            .collect();

        mf.set_metric(metrics);
    });

    mfs
```

**File:** rs/boundary_node/ic_boundary/src/metrics.rs (L214-220)
```rust
        // Get a snapshot of metrics
        let mut metric_families = self.registry.gather();

        // If we have a published snapshot - use it to remove the metrics not present anymore
        if let Some(snapshot) = self.published_registry_snapshot.load_full() {
            metric_families = remove_stale_metrics(snapshot, metric_families);
        }
```

**File:** rs/boundary_node/ic_boundary/src/metrics.rs (L493-506)
```rust
    // for /api/v2/subnet requests we extract subnet_id directly from extension
    let subnet_id = request.extensions().get::<SubnetId>().map(|x| x.get().0);
    let subnet_id_str = subnet_id.map(|x| x.to_string());

    let http_version = http_version(request.version());

    // Perform the request & measure duration
    let start_time = Instant::now();
    let response = next.run(request).await;
    let proc_duration = start_time.elapsed().as_secs_f64();

    // in case subnet_id=None (i.e. for /api/v2/canister/... request), we get the target subnet_id from the Subnet extension
    let subnet_id = subnet_id.or(response.extensions().get::<Arc<Subnet>>().map(|x| x.id));
    let subnet_id_str = subnet_id_str.or(subnet_id.map(|x| x.to_string()));
```

**File:** rs/boundary_node/ic_boundary/src/metrics.rs (L586-603)
```rust
        let labels = &[
            request_type,                     // x3
            status_code.as_str(),             // x27 but usually x8
            subnet_id_lbl.as_str(),           // x37 as of now
            error_cause_lbl.as_str(),         // x15 but usually x6
            cache_status_lbl.as_str(),        // x4
            cache_bypass_reason_lbl.as_str(), // x6 but since it relates only to BYPASS cache status -> total for 2 fields is x9
            retry_lbl,                        // x3
        ];

        counter.with_label_values(labels).inc();
        durationer.with_label_values(labels).observe(proc_duration);
        request_sizer
            .with_label_values(labels)
            .observe(ctx.request_size as f64);
        response_sizer
            .with_label_values(labels)
            .observe(response_size as f64);
```
