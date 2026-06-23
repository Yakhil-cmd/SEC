The code confirms this is a real vulnerability. Here is the complete analysis:

---

### Title
`RuleConfidentialityFormatter` Leaks `added_in_version` and `removed_in_version` for Non-Disclosed Rules to `RestrictedRead` Callers — (`rs/boundary_node/rate_limits/canister/confidentiality_formatting.rs`)

### Summary
`RuleConfidentialityFormatter::format` only nullifies `description` and `rule_raw` for non-disclosed rules, but leaves `added_in_version` and `removed_in_version` intact. Any unprivileged caller invoking the `get_rules_by_incident_id` query can observe the exact count of non-disclosed rules and their version lifecycle metadata.

### Finding Description

The `get_rules_by_incident_id` query method is callable by any principal with no authorization guard: [1](#0-0) 

Any caller who is not the authorized principal or an API boundary node receives `AccessLevel::RestrictedRead`: [2](#0-1) 

`IncidentGetter::get` builds `OutputRuleMetadata` with `added_in_version` and `removed_in_version` populated directly from storage: [3](#0-2) 

For `RestrictedRead` callers, `RuleConfidentialityFormatter::format` is applied, but it **only** nullifies `description` and `rule_raw`: [4](#0-3) 

`added_in_version` and `removed_in_version` are never touched. The `OutputRuleMetadata` type has `added_in_version` as a non-optional field and `removed_in_version` as `Option<Version>`: [5](#0-4) 

Both fields are passed through unchanged in the `From` conversion: [6](#0-5) 

The Candid interface exposes both fields in `OutputRuleMetadata`: [7](#0-6) 

**The existing test code itself confirms this behavior**: for a `RestrictedRead` caller, a non-disclosed rule returns `rule_raw: None, description: None` but `added_in_version: 1, removed_in_version: Some(3)` — the version metadata is fully exposed: [8](#0-7) 

### Impact Explanation

An unprivileged caller who knows (or guesses) an `incident_id` can:
1. **Count non-disclosed rules** — the number of `OutputRuleMetadata` entries with `rule_raw=None` reveals the exact cardinality of confidential rules in the incident.
2. **Read the version timeline** — `added_in_version` and `removed_in_version` reveal when each confidential rule became active and when it was deactivated, leaking the operational timeline of security-incident rate-limit rules.

This violates the intended confidentiality model. The attacker does not learn the actual rule content, but learns structural metadata that aids targeted probing.

### Likelihood Explanation

The path is trivially reachable: `get_rules_by_incident_id` is a public `#[query]` call, no authentication is required, and the leak is unconditional for any non-disclosed rule. The only prerequisite is knowing a valid `incident_id` UUID, which may be discoverable via other means (e.g., observing `get_config` responses which include `incident_id` per rule even for redacted rules). [9](#0-8) 

### Recommendation

In `RuleConfidentialityFormatter::format`, also zero/nullify `added_in_version` and `removed_in_version` when `disclosed_at.is_none()`:

```rust
// confidentiality_formatting.rs
if rule.disclosed_at.is_none() {
    rule.description = None;
    rule.rule_raw = None;
    rule.added_in_version = 0;       // or a sentinel value
    rule.removed_in_version = None;
}
```

The same fix should be applied to `ConfigConfidentialityFormatter` if `OutputConfig` ever gains version fields per rule.

### Proof of Concept

The existing test at `getter.rs` lines 449–520 already demonstrates the leak. A state-machine assertion confirming the invariant would be:

```rust
// For RestrictedRead caller, non-disclosed rules must not expose version metadata
for rule in response.iter() {
    if rule.rule_raw.is_none() {
        assert_eq!(rule.added_in_version, 0, "version must be hidden");
        assert_eq!(rule.removed_in_version, None, "removed version must be hidden");
    }
}
```

This assertion currently **fails** against the production code, confirming the vulnerability. [10](#0-9)

### Citations

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L136-146)
```rust
#[query]
fn get_rules_by_incident_id(incident_id: IncidentId) -> GetRulesByIncidentIdResponse {
    let caller_id = ic_cdk::api::caller();
    let response = with_canister_state(|state| {
        let access_resolver = AccessLevelResolver::new(caller_id, state.clone());
        let formatter = RuleConfidentialityFormatter;
        let getter = IncidentGetter::new(state, formatter, access_resolver);
        getter.get(&incident_id)
    })?;
    Ok(response)
}
```

**File:** rs/boundary_node/rate_limits/canister/access_control.rs (L38-55)
```rust
impl<R: CanisterApi> ResolveAccessLevel for AccessLevelResolver<R> {
    fn get_access_level(&self) -> AccessLevel {
        if let Some(authorized_principal) = self.canister_api.get_authorized_principal()
            && self.caller_id == authorized_principal
        {
            return AccessLevel::FullAccess;
        }

        let has_full_read_access = self
            .canister_api
            .is_api_boundary_node_principal(&self.caller_id);

        if has_full_read_access {
            return AccessLevel::FullRead;
        }

        AccessLevel::RestrictedRead
    }
```

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L182-198)
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
```

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L491-520)
```rust
        let getter_unauthorized = IncidentGetter::new(
            canister_state,
            RuleConfidentialityFormatter,
            create_mock_access_resolver(AccessLevel::RestrictedRead),
        );

        // Act & assert
        let response = getter_unauthorized.get(&incident_id.0.to_string()).unwrap();

        let rule_1 = api::OutputRuleMetadata {
            rule_id: rule_id_1.0.to_string(),
            incident_id: incident_id.0.to_string(),
            rule_raw: None,
            description: None,
            disclosed_at: None,
            added_in_version: 1,
            removed_in_version: Some(3),
        };
        let rule_2 = api::OutputRuleMetadata {
            rule_id: rule_id_2.0.to_string(),
            incident_id: incident_id.0.to_string(),
            rule_raw: Some(b"{\"b\": 2}".to_vec()),
            description: Some("verbose description 2".to_string()),
            disclosed_at: Some(1),
            added_in_version: 2,
            removed_in_version: Some(5),
        };
        // rules are not ordered in the response, so just search
        assert!(response.contains(&rule_1));
        assert!(response.contains(&rule_2));
```

**File:** rs/boundary_node/rate_limits/canister/confidentiality_formatting.rs (L34-42)
```rust
    fn format(&self, rule: OutputRuleMetadata) -> OutputRuleMetadata {
        let mut rule = rule;
        // Redact (hide) fields of non-disclosed rule
        if rule.disclosed_at.is_none() {
            rule.description = None;
            rule.rule_raw = None;
        }
        rule
    }
```

**File:** rs/boundary_node/rate_limits/canister/types.rs (L294-303)
```rust
#[derive(Debug, Clone)]
pub struct OutputRuleMetadata {
    pub id: RuleId,
    pub incident_id: IncidentId,
    pub rule_raw: Option<Vec<u8>>,
    pub description: Option<String>,
    pub disclosed_at: Option<Timestamp>,
    pub added_in_version: Version,
    pub removed_in_version: Option<Version>,
}
```

**File:** rs/boundary_node/rate_limits/canister/types.rs (L305-316)
```rust
impl From<OutputRuleMetadata> for api::OutputRuleMetadata {
    fn from(value: OutputRuleMetadata) -> Self {
        api::OutputRuleMetadata {
            rule_id: value.id.0.to_string(),
            incident_id: value.incident_id.0.to_string(),
            rule_raw: value.rule_raw,
            description: value.description,
            disclosed_at: value.disclosed_at,
            added_in_version: value.added_in_version,
            removed_in_version: value.removed_in_version,
        }
    }
```

**File:** rs/boundary_node/rate_limits/canister/interface.did (L18-23)
```text
type OutputRule = record {
  rule_id: RuleId;                   // Unique identifier for the rule
  incident_id: IncidentId;           // Identifier for the incident, to which the rule is related
  rule_raw: opt blob;                // Raw rule data (in binary format), expected to be a valid json object, none if the rule is currently confidential
  description: opt text;             // Textual description of the rule, none if the rule is currently confidential
};
```

**File:** rs/boundary_node/rate_limits/canister/interface.did (L40-48)
```text
type OutputRuleMetadata = record {
  rule_id: RuleId;                 // Unique identifier for the rule
  incident_id: IncidentId;         // Identifier for the incident, to which the rule is related
  rule_raw: opt blob;              // Raw rule data (binary format), expected to be a valid json object, none if the rule is currently confidential
  description: opt text;           // Textual description of the rule, none if the rule is currently confidential
  disclosed_at: opt Timestamp;     // Timestamp when the rule was disclosed, none if the rule is currently confidential
  added_in_version: Version;       // Version when the rule was added (became active)
  removed_in_version: opt Version; // Version when the rule was deactivated (removed), none if the rule is still active
};
```
