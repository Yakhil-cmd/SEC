### Title
`inspect_message` Guard Bypassed by Regular Query Calls; `incident_id` Leaked to Unauthorized Callers — (`rs/boundary_node/rate_limits/canister/canister.rs`, `getter.rs`, `confidentiality_formatting.rs`)

---

### Summary

The `inspect_message` hook that is intended to gate access to `get_config` only fires for **ingress (update/replicated-query) messages**. Regular IC query calls bypass it entirely. An unprivileged caller can therefore call `get_config` as a free, non-replicated query, receive a response, and observe the `incident_id` field for every rule — including non-disclosed ones — because the confidentiality formatter never redacts `incident_id`.

---

### Finding Description

**Root cause 1 — `inspect_message` does not cover regular query calls.**

The canister registers an `inspect_message` hook that checks whether the caller is authorized before accepting `get_config`: [1](#0-0) 

The constant that names the method is even called `REPLICATED_QUERY_METHOD`, signalling the developers' intent: [2](#0-1) 

However, per the IC interface specification, `inspect_message` is invoked **only for ingress messages** (update calls and replicated queries submitted through the ingress path). A plain `#[query]` call is not an ingress message; it is answered by a single replica without going through consensus, and `inspect_message` is never called. An unprivileged caller who sends a regular query call to `get_config` reaches the handler unconditionally.

**Root cause 2 — `incident_id` is not redacted for `RestrictedRead` callers.**

Inside `ConfigGetter::get`, when the caller is not `FullAccess`/`FullRead`, the formatter is applied: [3](#0-2) 

`ConfigConfidentialityFormatter::format` only nulls out `rule_raw` and `description` for non-disclosed rules; `incident_id` and `rule_id` pass through untouched: [4](#0-3) 

The `OutputRule → api::OutputRule` conversion confirms `incident_id` is always serialised into the wire response: [5](#0-4) 

---

### Impact Explanation

An unprivileged caller can:

1. Poll `get_config(None)` as a free query call and observe the `version` field.
2. When `version` increments, call `get_config(Some(new_version))` and read the `incident_id` values of every newly added rule, even those with `disclosed_at = None`.
3. Correlate the new UUID with any out-of-band knowledge (e.g., public incident trackers, on-chain activity) to infer that a security incident is actively being mitigated.
4. Observe the exact timestamp (`active_since`) at which the new config became active.

The attacker does **not** learn `rule_raw` or `description`, so they cannot read the exact rate-limit expression. The $1M+ financial-race claim therefore requires the attacker to already possess independent knowledge of the exploit being mitigated — the leak alone does not hand them the exploit. The concrete, directly achievable impact is:

- **Timing oracle**: the moment a new rate-limit config is committed is observable at zero cost.
- **Incident-UUID disclosure**: UUIDs of undisclosed security incidents are exposed to any anonymous caller.

---

### Likelihood Explanation

The attack requires no privileges, no cycles, and no special tooling — a single HTTP query to the canister endpoint suffices. The `inspect_message` bypass is a structural property of the IC protocol, not a configuration error, so it is reliably exploitable on mainnet.

---

### Recommendation

1. **Remove the `inspect_message` guard for `get_config` entirely** — it provides no protection for query calls. Instead, enforce access control inside the handler itself: if the resolved access level is `RestrictedRead`, return an explicit authorization error rather than a redacted response.
2. **Redact `incident_id` in `ConfigConfidentialityFormatter::format`** for rules where `disclosed_at.is_none()`, replacing it with a placeholder (e.g., empty string or a stable hash) so that the UUID is not observable before disclosure.
3. If public read of the config is intentional for some callers, document the exact access-level semantics and ensure `incident_id` is treated as confidential data on par with `rule_raw`.

---

### Proof of Concept

```
# Any anonymous caller, zero cycles, no authentication:
dfx canister call <rate_limit_canister_id> get_config '(null)' --query
# → returns ConfigResponse { version: N, active_since: T, config: { rules: [...] } }

# After version increments:
dfx canister call <rate_limit_canister_id> get_config '(opt N)' --query
# → returns rules including incident_id for non-disclosed rules
```

The `--query` flag sends a non-replicated query call, bypassing `inspect_message`. The response includes `incident_id` for every rule regardless of `disclosed_at` status. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L31-31)
```rust
const REPLICATED_QUERY_METHOD: &str = "get_config";
```

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L48-55)
```rust
    if called_method == REPLICATED_QUERY_METHOD {
        if has_full_access || has_full_read_access {
            ic_cdk::api::call::accept_message();
        } else {
            ic_cdk::api::trap(
                "message_inspection_failed: method call is prohibited in the current context",
            );
        }
```

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L110-120)
```rust
#[query]
fn get_config(version: Option<Version>) -> GetConfigResponse {
    let caller_id = ic_cdk::api::caller();
    let response = with_canister_state(|state| {
        let access_resolver = AccessLevelResolver::new(caller_id, state.clone());
        let formatter = ConfigConfidentialityFormatter;
        let getter = ConfigGetter::new(state, formatter, access_resolver);
        getter.get(&version)
    })?;
    Ok(response)
}
```

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L92-148)
```rust
    fn get(&self, version: &Option<Version>) -> Result<Self::Output, Self::Error> {
        let latest_version = self
            .canister_api
            .get_version()
            .ok_or_else(|| GetConfigError::NoExistingConfigsFound)?;

        let version = version.unwrap_or(latest_version);

        let stored_config = self
            .canister_api
            .get_config(version)
            .ok_or_else(|| GetConfigError::NotFound(version))?;

        let mut rules: Vec<OutputRule> = vec![];

        for rule_id in stored_config.rule_ids.iter() {
            let rule = self.canister_api.get_rule(rule_id).ok_or_else(|| {
                // This error should never happen, it means that the stored data is inconsistent.
                GetConfigError::Internal(anyhow::anyhow!("Rule with id={rule_id} not found"))
            })?;

            let output_rule = OutputRule {
                id: *rule_id,
                incident_id: rule.incident_id,
                rule_raw: Some(rule.rule_raw),
                description: Some(rule.description),
                disclosed_at: rule.disclosed_at,
            };

            rules.push(output_rule);
        }

        let config = OutputConfig {
            schema_version: stored_config.schema_version,
            is_redacted: false,
            rules,
        };

        let is_authorized_viewer = self.access_resolver.get_access_level()
            == AccessLevel::FullAccess
            || self.access_resolver.get_access_level() == AccessLevel::FullRead;

        if is_authorized_viewer {
            return Ok(api::ConfigResponse {
                version,
                active_since: stored_config.active_since,
                config: config.into(),
            });
        }

        // Hide non-disclosed rules from unauthorized viewers.
        Ok(api::ConfigResponse {
            version,
            active_since: stored_config.active_since,
            config: self.formatter.format(config).into(),
        })
    }
```

**File:** rs/boundary_node/rate_limits/canister/confidentiality_formatting.rs (L17-28)
```rust
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

**File:** rs/boundary_node/rate_limits/canister/types.rs (L78-87)
```rust
impl From<OutputRule> for api::OutputRule {
    fn from(value: OutputRule) -> Self {
        api::OutputRule {
            description: value.description,
            rule_id: value.id.to_string(),
            incident_id: value.incident_id.to_string(),
            rule_raw: value.rule_raw,
        }
    }
}
```
