### Title
Information Disclosure of Non-Disclosed Rule/Incident Identifiers to RestrictedRead Callers — (`rs/boundary_node/rate_limits/canister/getter.rs`, `rs/boundary_node/rate_limits/canister/confidentiality_formatting.rs`)

---

### Summary

The `ConfigConfidentialityFormatter` and `RuleConfidentialityFormatter` only redact `rule_raw` and `description` for non-disclosed rules, but leave `rule_id` and `incident_id` fully populated in responses to `RestrictedRead` (unprivileged) callers. Combined with the fact that `get_config`, `get_rule_by_id`, and `get_rules_by_incident_id` are all `#[query]` methods accessible via non-replicated query calls (which bypass `inspect_message`), an anonymous caller can enumerate the complete structural topology of all rate-limit rules and incidents — including those never disclosed.

---

### Finding Description

**Step 1 — Formatter does not redact identifiers.**

`ConfigConfidentialityFormatter::format` only nulls out `rule_raw` and `description` for non-disclosed rules: [1](#0-0) 

`rule_id` and `incident_id` are never touched. The existing test in `getter.rs` explicitly asserts this behavior — a `RestrictedRead` caller receives `rule_id` and `incident_id` populated for a non-disclosed rule: [2](#0-1) 

**Step 2 — `inspect_message` does not protect query calls.**

The `inspect_message` hook only fires for ingress messages (update calls and replicated queries). All three getter methods are annotated `#[query]`: [3](#0-2) [4](#0-3) [5](#0-4) 

Non-replicated query calls bypass `inspect_message` entirely. The guard at lines 48–55 of `canister.rs` that restricts `get_config` to `FullAccess`/`FullRead` only applies when the method is called as a replicated query (ingress): [6](#0-5) 

**Step 3 — `get_rules_by_incident_id` is also unguarded for RestrictedRead.**

`IncidentGetter::get` applies `RuleConfidentialityFormatter`, which again only redacts `rule_raw`/`description`, leaving `rule_id`, `incident_id`, `added_in_version`, and `removed_in_version` exposed: [7](#0-6) 

**Attack path:**
1. Anonymous caller issues non-replicated query `get_config(version)` for `version = 1..N` → collects all `rule_id` and `incident_id` values, including those of non-disclosed rules.
2. For each collected `incident_id`, calls `get_rules_by_incident_id(incident_id)` → receives `rule_id`, `incident_id`, `added_in_version`, `removed_in_version` for every rule in that incident.

---

### Impact Explanation

The attacker learns the complete structural topology: all rule UUIDs, all incident UUIDs, and the full version history (`added_in_version`, `removed_in_version`) of every rule — including those intentionally kept confidential. While `rule_raw` (the actual rate-limit logic) is not exposed, the structural metadata enables:
- Knowing exactly how many undisclosed rules exist and which incidents they belong to.
- Targeted probing: if rule IDs appear in logs, metrics, or other canister interfaces, the attacker can correlate them.
- Timing/side-channel inference: knowing which versions introduced or removed rules narrows the search space for rule content.

---

### Likelihood Explanation

The attack requires no privileges, no keys, and no social engineering. Any anonymous principal can issue query calls to the canister. The path is fully local-testable and requires only standard IC query calls.

---

### Recommendation

The `ConfigConfidentialityFormatter` and `RuleConfidentialityFormatter` must also redact `rule_id` and `incident_id` for non-disclosed rules when the caller is `RestrictedRead`. Specifically:

- In `ConfigConfidentialityFormatter::format`: replace the `rule_id` with a placeholder (e.g., empty string or a stable hash) and null out `incident_id` when `disclosed_at.is_none()`.
- In `RuleConfidentialityFormatter::format`: apply the same redaction to `rule_id` and `incident_id`.
- Update the `IncidentGetter` to return an error or empty response for non-disclosed incidents when the caller is `RestrictedRead`, rather than returning redacted-but-enumerable rule metadata. [8](#0-7) 

---

### Proof of Concept

```rust
// Unit test sketch (mirrors existing test structure in getter.rs)
let canister_state = CanisterState::from_static();
let rule_id = RuleId(Uuid::new_v4());
let incident_id = IncidentId(Uuid::new_v4());

canister_state.upsert_rule(rule_id, StorableRule {
    incident_id,
    rule_raw: b"{\"secret\": true}".to_vec(),
    description: "confidential rule".to_string(),
    disclosed_at: None,  // NOT disclosed
    added_in_version: 1,
    removed_in_version: None,
});
canister_state.add_config(1, StorableConfig {
    schema_version: 1, active_since: 0, rule_ids: vec![rule_id],
});

let getter = ConfigGetter::new(
    canister_state,
    ConfigConfidentialityFormatter,
    create_mock_access_resolver(AccessLevel::RestrictedRead),
);

let response = getter.get(&Some(1)).unwrap();
let rule = &response.config.rules[0];

// These assertions PASS — identifiers are leaked to unprivileged caller
assert!(!rule.rule_id.is_empty());       // rule_id is exposed
assert!(!rule.incident_id.is_empty());   // incident_id is exposed
assert!(rule.rule_raw.is_none());        // content is redacted
```

This matches the existing test at lines 358–384 of `getter.rs`, which already documents and asserts this behavior. [9](#0-8)

### Citations

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

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L182-199)
```rust
            let output_rule = OutputRuleMetadata {
                id: rule_id,
                incident_id,
                rule_raw: Some(stored_rule.rule_raw),
                description: Some(stored_rule.description),
                disclosed_at: stored_rule.disclosed_at,
                added_in_version: stored_rule.added_in_version,
                removed_in_version: stored_rule.removed_in_version,
            };

            if is_authorized_viewer {
                output_rules.push(output_rule.into());
            } else {
                // Hide non-disclosed rule from unauthorized viewers.
                let output_rule = self.formatter.format(output_rule);
                output_rules.push(output_rule.into());
            }
        }
```

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L358-384)
```rust
        let response = getter_unauthorized.get(&Some(1)).unwrap();
        // config is redacted and non-disclosed rules are hidden
        assert_eq!(
            response,
            api::ConfigResponse {
                version,
                active_since,
                config: api::OutputConfig {
                    schema_version,
                    is_redacted: true,
                    rules: vec![
                        api::OutputRule {
                            rule_id: rule_id_1.0.to_string(),
                            incident_id: incident_id.0.to_string(),
                            rule_raw: None,
                            description: None,
                        },
                        api::OutputRule {
                            rule_id: rule_id_2.0.to_string(),
                            incident_id: incident_id.0.to_string(),
                            rule_raw: Some(b"{\"b\": 2}".to_vec()),
                            description: Some("verbose description 2".to_string()),
                        }
                    ]
                }
            }
        );
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

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L110-111)
```rust
#[query]
fn get_config(version: Option<Version>) -> GetConfigResponse {
```

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L123-124)
```rust
#[query]
fn get_rule_by_id(rule_id: RuleId) -> GetRuleByIdResponse {
```

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L137-138)
```rust
fn get_rules_by_incident_id(incident_id: IncidentId) -> GetRulesByIncidentIdResponse {
    let caller_id = ic_cdk::api::caller();
```
