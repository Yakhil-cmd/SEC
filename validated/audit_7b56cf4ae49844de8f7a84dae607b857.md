The code confirms this is a real vulnerability. Here is the analysis:

---

### Title
Metrics Endpoint Leaks Count of Confidential Rate-Limit Rules and Incidents to Anonymous Callers — (`rs/boundary_node/rate_limits/canister/metrics.rs`, `canister.rs`)

### Summary
The `/metrics` endpoint of the rate-limit canister is publicly accessible with no authorization check and exposes `active_rules_count` and `incidents_count` that are computed directly from raw canister state — including confidential (undisclosed) rules and incidents. This bypasses the confidentiality model enforced by `ConfigConfidentialityFormatter` and `RuleConfidentialityFormatter`.

### Finding Description

The `http_request` query handler routes `/metrics` to `export_metrics_as_http_response` with no caller identity check: [1](#0-0) 

`export_metrics_as_http_response` calls `recompute_metrics`, which sets the following gauges directly from raw state: [2](#0-1) 

`active_rules_count()` counts **all** rule IDs in the latest config, regardless of disclosure status: [3](#0-2) 

`incidents_count()` counts **all** incidents in the map, regardless of disclosure status: [4](#0-3) 

The confidentiality formatters only redact `description` and `rule_raw` fields when serving `get_config` / `get_rule_by_id` responses — they have no effect on the metrics path: [5](#0-4) 

The metrics also expose `active_config_version` (increments on every `add_config` call) and `stored_configs_count`, both of which change immediately when a confidential rule is added. [6](#0-5) 

### Impact Explanation

An anonymous caller polling `GET /metrics` can observe:
- `active_rules_count` — total rules in the active config, including undisclosed ones
- `stored_incidents_count` — total incidents, including undisclosed ones
- `active_config_version` — increments with every `add_config`, including confidential ones
- `stored_configs_count` — same

This directly violates the stated invariant that the existence and count of confidential rate-limit rules must not be observable by unprivileged callers. An attacker conducting an abuse campaign can monitor these metrics and detect the moment a new blocking rule is deployed in response to their activity, allowing them to pause, rotate infrastructure, or time their attack to precede rule activation.

### Likelihood Explanation

The exploit requires only an unauthenticated HTTP query to a publicly accessible canister endpoint. No special privileges, keys, or network position are needed. The endpoint is designed to be scraped by Prometheus, so it is intentionally reachable.

### Recommendation

Apply the same confidentiality filtering to the metrics that is applied to `get_config`. Specifically:
- `active_rules_count` should count only rules where `disclosed_at.is_some()`
- `incidents_count` should count only incidents where at least one associated rule is disclosed
- Alternatively, remove these sensitive gauges from the public metrics endpoint entirely and expose them only to privileged scrapers

### Proof of Concept

State-machine test:
1. Deploy the canister.
2. Call `add_config` with a rule that has no `disclose_rules` call (confidential).
3. Call `http_request GET /metrics` as the anonymous principal.
4. Assert that `active_rules_count` and `stored_incidents_count` in the response body reflect the newly added confidential rule/incident.
5. Observe that `get_config` for the same version redacts `rule_raw` and `description` — confirming the metrics bypass the confidentiality layer entirely.

### Citations

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L184-192)
```rust
#[query(
    hidden = true,
    decode_with = "candid::decode_one_with_decoding_quota::<100000,_>"
)]
fn http_request(request: HttpRequest) -> HttpResponse {
    match request.path() {
        "/metrics" => with_canister_state(|state| {
            with_metrics_registry(|registry| export_metrics_as_http_response(registry, state))
        }),
```

**File:** rs/boundary_node/rate_limits/canister/metrics.rs (L60-71)
```rust
        let active_rate_limit_rules_count = IntGauge::new(
            "active_rules_count",
            "Number of rate-limit rules in the active configuration",
        )?;

        let incidents_count =
            IntGauge::new("stored_incidents_count", "Number of stored incidents")?;

        let configs_count = IntGauge::new(
            "stored_configs_count",
            "Number of stored rate-limit configurations",
        )?;
```

**File:** rs/boundary_node/rate_limits/canister/metrics.rs (L142-158)
```rust
pub fn recompute_metrics(canister_api: impl CanisterApi) {
    METRICS.with(|cell| {
        let mut cell = cell.borrow_mut();

        let memory = (ic_cdk::stable::stable_size() * WASM_PAGE_SIZE_IN_BYTES) as f64;

        cell.stable_memory_size.borrow_mut().set(memory);
        cell.api_boundary_nodes_count
            .set(canister_api.api_boundary_nodes_count() as i64);
        cell.active_version
            .set(canister_api.get_version().unwrap_or(0) as i64);
        cell.configs_count.set(canister_api.configs_count() as i64);
        cell.incidents_count
            .set(canister_api.incidents_count() as i64);
        cell.active_rate_limit_rules_count
            .set(canister_api.active_rules_count() as i64);
    });
```

**File:** rs/boundary_node/rate_limits/canister/state.rs (L139-141)
```rust
    fn incidents_count(&self) -> u64 {
        self.incidents.with(|cell| cell.borrow().len())
    }
```

**File:** rs/boundary_node/rate_limits/canister/state.rs (L143-150)
```rust
    fn active_rules_count(&self) -> u64 {
        self.configs.with(|cell| {
            let configs = cell.borrow();
            configs
                .last_key_value()
                .map_or(0, |(_, value)| value.rule_ids.len() as u64)
        })
    }
```

**File:** rs/boundary_node/rate_limits/canister/confidentiality_formatting.rs (L14-28)
```rust
impl ConfidentialityFormatting for ConfigConfidentialityFormatter {
    type Input = OutputConfig;

    fn format(&self, config: OutputConfig) -> OutputConfig {
        let mut config = config;
        config.is_redacted = true;
        // Redact (hide) fields of non-disclosed rules
        config.rules.iter_mut().for_each(|rule| {
            if rule.disclosed_at.is_none() {
                rule.description = None;
                rule.rule_raw = None;
            }
        });
        config
    }
```
