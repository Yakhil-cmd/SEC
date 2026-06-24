### Title
`incident_id` Leaked to `RestrictedRead` Callers for Non-Disclosed Rules via Regular Query — (`rs/boundary_node/rate_limits/canister/confidentiality_formatting.rs`)

### Summary

`ConfigConfidentialityFormatter::format` redacts only `description` and `rule_raw` for non-disclosed rules, leaving `incident_id` intact in the response. Any caller — including `RestrictedRead` — can invoke `get_config` as a regular (non-replicated) query, which bypasses `inspect_message` entirely (IC protocol property). The result is that `incident_id` values for every non-disclosed rule are returned to unprivileged callers.

---

### Finding Description

**1. `inspect_message` does not fire for regular queries**

`inspect_message` is an IC ingress-message hook; it fires only for update calls and replicated (certified) queries — never for regular query calls. The canister defines: [1](#0-0) 

```rust
const REPLICATED_QUERY_METHOD: &str = "get_config";
```

and guards it inside `inspect_message`: [2](#0-1) 

This guard is entirely inert for regular query calls. `get_config` is annotated `#[query]`: [3](#0-2) 

Any caller — including `RestrictedRead` — can invoke it as a regular query with no `inspect_message` interception.

**2. `get_config` has no in-function access control**

The function body resolves the access level and delegates to `ConfigGetter::get`, which applies the formatter for non-`FullAccess`/`FullRead` callers: [4](#0-3) 

**3. `ConfigConfidentialityFormatter` does not nullify `incident_id`** [5](#0-4) 

Only `description` and `rule_raw` are set to `None`. `incident_id` (and `rule_id`) survive the redaction pass unchanged.

**4. The `OutputRule` conversion preserves `incident_id`** [6](#0-5) 

**5. The existing test confirms the leak**

The test for `RestrictedRead` explicitly asserts `incident_id` is present in the response for a non-disclosed rule: [7](#0-6) 

---

### Impact Explanation

A `RestrictedRead` caller (any anonymous or non-privileged principal) can:
- Enumerate all `incident_id` UUIDs present in the active config, including those for rules that have never been publicly disclosed.
- Map incident IDs to their ordinal positions in the config, enabling correlation with external incident-tracking systems or public disclosures.
- Repeat the call across versions to track when new undisclosed incidents are added.

The actual rule payload (`rule_raw`, `description`) remains hidden, so the attacker cannot reconstruct the rate-limit logic itself. Impact is scoped to incident-ID enumeration.

---

### Likelihood Explanation

The attack requires no special privilege, no key material, and no social engineering. Any principal can issue a regular query call to `get_config`. The bypass of `inspect_message` is a fundamental IC protocol property, not a configuration choice. The `incident_id` exposure is confirmed by the canister's own unit test.

---

### Recommendation

1. **Nullify `incident_id` in `ConfigConfidentialityFormatter::format`** for rules where `disclosed_at.is_none()`:

```rust
if rule.disclosed_at.is_none() {
    rule.description = None;
    rule.rule_raw = None;
    rule.incident_id = IncidentId(Uuid::nil()); // or a sentinel value
}
```

Apply the same fix to `RuleConfidentialityFormatter`.

2. **Add an explicit access-level guard inside `get_config`** (and the other query handlers) if `RestrictedRead` callers are not intended to call these methods at all, since `inspect_message` cannot enforce this for regular queries.

---

### Proof of Concept

```
1. Authorized principal calls add_config with rules linked to known incident UUIDs (some undisclosed).
2. RestrictedRead caller issues a regular query:
     dfx canister call rate_limits_canister get_config '(null)' --query
3. Response contains, for each non-disclosed rule:
     rule_id: "<uuid>", incident_id: "<uuid>", rule_raw: null, description: null
4. incident_id values for non-disclosed rules are fully visible.
5. Differential: FullRead response has identical incident_id values → confirmed leak.
```

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

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L130-147)
```rust
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
```

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L368-375)
```rust
                    rules: vec![
                        api::OutputRule {
                            rule_id: rule_id_1.0.to_string(),
                            incident_id: incident_id.0.to_string(),
                            rule_raw: None,
                            description: None,
                        },
                        api::OutputRule {
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
