Looking at the key files to verify the exploit path precisely.

### Title
Non-Disclosed Rate-Limit Rule Metadata Exposed to Unprivileged Callers via Unguarded Query Methods — (`rs/boundary_node/rate_limits/canister/canister.rs`, `getter.rs`, `confidentiality_formatting.rs`)

### Summary

`get_rule_by_id` and `get_rules_by_incident_id` are `#[query]` methods that are never listed in `UPDATE_METHODS` or `REPLICATED_QUERY_METHOD`. On the Internet Computer, `inspect_message` is **only invoked for ingress update messages**, never for non-replicated query calls. Any anonymous caller can therefore invoke these methods as query calls, bypassing `inspect_message` entirely. The `RuleConfidentialityFormatter` only redacts `rule_raw` and `description` for non-disclosed rules — `rule_id`, `incident_id`, `added_in_version`, and `removed_in_version` are always returned. Additionally, `get_config` as a non-replicated query call also bypasses `inspect_message`, giving the attacker all `rule_id` and `incident_id` values needed to enumerate the full rule/incident graph.

### Finding Description

**Step 1 — `inspect_message` does not protect query calls.**

`inspect_message` is registered as the ingress filter hook: [1](#0-0) 

The IC protocol only invokes this hook for ingress update messages. Non-replicated query calls skip it unconditionally. The `else` branch that traps "all other calls" therefore never fires for query-type invocations.

**Step 2 — `get_rule_by_id` and `get_rules_by_incident_id` are `#[query]` methods not listed in any guard.** [2](#0-1) 

Neither method appears in `UPDATE_METHODS` or equals `REPLICATED_QUERY_METHOD`: [3](#0-2) 

**Step 3 — `RuleConfidentialityFormatter` leaves identifying metadata unredacted.**

For a non-disclosed rule (`disclosed_at.is_none()`), only `description` and `rule_raw` are cleared: [4](#0-3) 

`rule_id`, `incident_id`, `added_in_version`, and `removed_in_version` are always serialized into the response: [5](#0-4) 

**Step 4 — `RuleGetter.get()` confirms the leak path.**

For an unauthorized (`RestrictedRead`) caller, the formatter is applied but the resulting struct still carries all metadata fields: [6](#0-5) 

**Step 5 — UUID enumeration is solved by `get_config` as a query call.**

`get_config` is also a `#[query]` method. Called as a non-replicated query, it bypasses `inspect_message`. `ConfigConfidentialityFormatter` similarly leaves `rule_id` and `incident_id` intact for non-disclosed rules: [7](#0-6) 

The `From<OutputRule> for api::OutputRule` conversion always includes both IDs: [8](#0-7) 

So the attacker first calls `get_config` as a query to harvest all `rule_id`/`incident_id` values, then calls `get_rule_by_id` / `get_rules_by_incident_id` to obtain `added_in_version` and `removed_in_version` for each non-disclosed rule.

### Impact Explanation

An unprivileged (anonymous) caller learns:
- The existence of every non-disclosed rule and incident
- The incident-to-rule mapping (which rules belong to which incident)
- The exact canister versions at which each rule was added and removed

This reveals the structure and timing of active incident response before public disclosure, enabling a sophisticated attacker to detect when new rate-limit rules are being deployed and adjust evasion behavior accordingly.

### Likelihood Explanation

The exploit requires only standard IC query calls — no special tooling, no privileged access, no brute-forcing. The UUID enumeration prerequisite is fully satisfied by calling `get_config` as a query first. The path is entirely local-testable and requires no network-level attack.

### Recommendation

1. **Remove `get_rule_by_id` and `get_rules_by_incident_id` from the public query interface**, or convert them to update methods and add them to `UPDATE_METHODS` with appropriate authorization checks inside the handler (not just in `inspect_message`).
2. **Add in-handler authorization checks** inside `get_rule_by_id`, `get_rules_by_incident_id`, and `get_config` that reject `RestrictedRead` callers at the application level, independent of `inspect_message`.
3. **Redact `rule_id` and `incident_id`** in `RuleConfidentialityFormatter` and `ConfigConfidentialityFormatter` for non-disclosed rules, so that even if the methods remain callable, existence of a non-disclosed rule is not confirmed.
4. Do not rely solely on `inspect_message` for confidentiality enforcement — it is a pre-consensus optimization hint, not a security boundary for query calls.

### Proof of Concept

```
# 1. Harvest all rule/incident IDs (including non-disclosed) via get_config query
dfx canister call rate_limits_canister get_config '(null)' --query
# Response includes rule_id and incident_id for ALL rules, disclosed or not.

# 2. For each non-disclosed rule_id obtained above:
dfx canister call rate_limits_canister get_rule_by_id '("<non-disclosed-uuid>")' --query
# Response: rule_id, incident_id, added_in_version, removed_in_version are present.
# Only rule_raw and description are null.

# 3. For each incident_id:
dfx canister call rate_limits_canister get_rules_by_incident_id '("<incident-uuid>")' --query
# Response: full metadata for all rules in the incident, including non-disclosed ones.
```

The existing unit test in `getter.rs` (lines 433–446) already documents that `rule_id`, `incident_id`, `added_in_version`, and `removed_in_version` are present in the `RestrictedRead` response for a non-disclosed rule: [9](#0-8)

### Citations

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L30-31)
```rust
const UPDATE_METHODS: [&str; 2] = ["add_config", "disclose_rules"];
const REPLICATED_QUERY_METHOD: &str = "get_config";
```

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L34-68)
```rust
#[inspect_message]
fn inspect_message() {
    // In order for this hook to succeed, accept_message() must be invoked.
    let caller_id: Principal = ic_cdk::api::caller();
    let called_method = ic_cdk::api::call::method_name();

    let (has_full_access, has_full_read_access) = with_canister_state(|state| {
        let authorized_principal = state.get_authorized_principal();
        (
            Some(caller_id) == authorized_principal,
            state.is_api_boundary_node_principal(&caller_id),
        )
    });

    if called_method == REPLICATED_QUERY_METHOD {
        if has_full_access || has_full_read_access {
            ic_cdk::api::call::accept_message();
        } else {
            ic_cdk::api::trap(
                "message_inspection_failed: method call is prohibited in the current context",
            );
        }
    } else if UPDATE_METHODS.contains(&called_method.as_str()) {
        if has_full_access {
            ic_cdk::api::call::accept_message();
        } else {
            ic_cdk::api::trap("message_inspection_failed: unauthorized caller");
        }
    } else {
        // All others calls are rejected
        ic_cdk::api::trap(
            "message_inspection_failed: method call is prohibited in the current context",
        );
    }
}
```

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L123-146)
```rust
#[query]
fn get_rule_by_id(rule_id: RuleId) -> GetRuleByIdResponse {
    let caller_id = ic_cdk::api::caller();
    let response = with_canister_state(|state| {
        let access_resolver = AccessLevelResolver::new(caller_id, state.clone());
        let formatter = RuleConfidentialityFormatter;
        let getter = RuleGetter::new(state, formatter, access_resolver);
        getter.get(&rule_id)
    })?;
    Ok(response)
}

/// Retrieves all rate-limit rules associated with a specific incident ID, applying confidentiality formatting, based on caller's access level and rule's confidentiality status
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

**File:** rs/boundary_node/rate_limits/canister/confidentiality_formatting.rs (L14-29)
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
}
```

**File:** rs/boundary_node/rate_limits/canister/confidentiality_formatting.rs (L31-43)
```rust
impl ConfidentialityFormatting for RuleConfidentialityFormatter {
    type Input = OutputRuleMetadata;

    fn format(&self, rule: OutputRuleMetadata) -> OutputRuleMetadata {
        let mut rule = rule;
        // Redact (hide) fields of non-disclosed rule
        if rule.disclosed_at.is_none() {
            rule.description = None;
            rule.rule_raw = None;
        }
        rule
    }
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

**File:** rs/boundary_node/rate_limits/canister/types.rs (L305-317)
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
}
```

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L224-245)
```rust
        let output_rule = OutputRuleMetadata {
            id: rule_id,
            incident_id: stored_rule.incident_id,
            rule_raw: Some(stored_rule.rule_raw),
            description: Some(stored_rule.description),
            disclosed_at: stored_rule.disclosed_at,
            added_in_version: stored_rule.added_in_version,
            removed_in_version: stored_rule.removed_in_version,
        };

        let is_authorized_viewer = self.access_resolver.get_access_level()
            == AccessLevel::FullAccess
            || self.access_resolver.get_access_level() == AccessLevel::FullRead;

        if is_authorized_viewer {
            return Ok(output_rule.into());
        }

        // Hide non-disclosed rules from unauthorized viewers.
        let output_rule = self.formatter.format(output_rule);

        Ok(output_rule.into())
```

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L433-447)
```rust
        let response = getter_unauthorized.get(&rule_id.0.to_string()).unwrap();
        // rule fields are hidden
        assert_eq!(
            response,
            api::OutputRuleMetadata {
                rule_id: rule_id.0.to_string(),
                incident_id: incident_id.0.to_string(),
                rule_raw: None,
                description: None,
                disclosed_at: None,
                added_in_version: 1,
                removed_in_version: Some(3),
            }
        );
    }
```
