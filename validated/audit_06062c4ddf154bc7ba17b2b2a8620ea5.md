### Title
`inspect_message` Access Gate Bypassed via Non-Replicated Query Calls to `get_rule_by_id` and `get_rules_by_incident_id` — (File: `rs/boundary_node/rate_limits/canister/canister.rs`)

---

### Summary

The rate-limits canister uses an `inspect_message` hook as its primary access control gate, explicitly blocking all callers except authorized principals and API boundary nodes from calling `get_config` (replicated query). However, two sibling read methods — `get_rule_by_id` and `get_rules_by_incident_id` — are `#[query]` methods callable as non-replicated queries, which entirely bypass `inspect_message` in the IC execution model. Any unprivileged ingress sender can therefore query individual rate-limit rules and incident rule sets without going through the intended gate, defeating the confidentiality intent for the `RestrictedRead` access tier.

---

### Finding Description

**Access control design:**

The canister defines three access levels in `access_control.rs`:
- `FullAccess` — the single authorized principal (write + full read)
- `FullRead` — registered API boundary nodes (full read)
- `RestrictedRead` — everyone else [1](#0-0) 

**The `inspect_message` gate:**

The `inspect_message` hook in `canister.rs` is the declared primary gate. It accepts only three methods as ingress messages: `get_config` (replicated query), `add_config`, and `disclose_rules`. All other method names are explicitly trapped with `"method call is prohibited in the current context"`. [2](#0-1) 

**The bypass:**

In the IC execution model, `inspect_message` is invoked only for **ingress messages** (update calls and replicated queries). Non-replicated query calls — the standard path for `#[query]` methods — bypass `inspect_message` entirely. `get_rule_by_id` and `get_rules_by_incident_id` are both declared `#[query]`: [3](#0-2) 

Any caller can invoke these two methods as non-replicated queries. The `inspect_message` trap for "all other methods" never fires for them.

**Internal redaction is insufficient to close the gap:**

Inside the getter functions, `RuleConfidentialityFormatter` redacts only `rule_raw` and `description` for non-disclosed rules. It still returns `rule_id`, `incident_id`, `disclosed_at`, `added_in_version`, and `removed_in_version` to `RestrictedRead` callers: [4](#0-3) 

For **disclosed** rules, `RuleConfidentialityFormatter` redacts nothing at all — full `rule_raw` and `description` are returned to any caller. [5](#0-4) 

**Obtaining rule/incident UUIDs:**

`get_config` is also a `#[query]` method. When called as a non-replicated query (bypassing `inspect_message`), `ConfigGetter` returns a redacted config that still includes all `rule_id` and `incident_id` values for every rule — disclosed and non-disclosed alike: [6](#0-5) 

This gives an unprivileged caller the full UUID inventory needed to enumerate rules via `get_rule_by_id` and `get_rules_by_incident_id`.

---

### Impact Explanation

An unprivileged caller (no keys, no boundary-node registration) can:

1. Call `get_config` as a non-replicated query → receive a redacted config containing every `rule_id` and `incident_id` in the canister.
2. Call `get_rules_by_incident_id` for each `incident_id` → receive all rules in each incident, with full `rule_raw`/`description` for disclosed rules and structural metadata (`rule_id`, `incident_id`, version range) for non-disclosed rules.
3. Call `get_rule_by_id` for each `rule_id` → same per-rule disclosure.

The `inspect_message` gate was the intended barrier preventing `RestrictedRead` callers from accessing this data. That barrier is completely absent for the non-replicated query path. Disclosed rate-limit rules — which describe how boundary nodes throttle traffic — are fully readable by any external caller, enabling an attacker to craft traffic patterns that evade active rate-limit policies. Non-disclosed rules leak structural metadata (existence, incident association, version lifecycle) that reveals ongoing security-incident response activity.

---

### Likelihood Explanation

The attack requires no privileges, no keys, and no special network position. Any IC user with the canister ID can issue a non-replicated query call. The IC HTTP endpoint at `/api/v2/canister/<id>/query` accepts these calls from any authenticated or anonymous principal. The UUID enumeration step is a straightforward two-step query sequence. Likelihood is **high**.

---

### Recommendation

Add an explicit `RestrictedRead` rejection at the top of `get_rule_by_id` and `get_rules_by_incident_id` in `canister.rs`, mirroring the intent of the `inspect_message` gate:

```rust
#[query]
fn get_rule_by_id(rule_id: RuleId) -> GetRuleByIdResponse {
    let caller_id = ic_cdk::api::caller();
    // Reject callers who are not FullAccess or FullRead
    with_canister_state(|state| {
        let access_resolver = AccessLevelResolver::new(caller_id, state.clone());
        if access_resolver.get_access_level() == AccessLevel::RestrictedRead {
            return Err(GetEntityError::Unauthorized);
        }
        // ... existing getter logic
    })?;
    Ok(response)
}
```

Apply the same guard to `get_rules_by_incident_id`. Alternatively, add `"get_rule_by_id"` and `"get_rules_by_incident_id"` to the `inspect_message` allowlist under the `FullAccess || FullRead` branch — but note that `inspect_message` still will not fire for non-replicated query calls, so the in-function guard is the only reliable fix.

---

### Proof of Concept

```
# Step 1 – enumerate all rule/incident UUIDs (non-replicated query, bypasses inspect_message)
dfx canister call <rate_limit_canister_id> get_config '(null)' --query
# → returns redacted OutputConfig with all rule_ids and incident_ids visible

# Step 2 – read full content of a disclosed rule (no authorization required)
dfx canister call <rate_limit_canister_id> get_rule_by_id '("<rule_uuid>")' --query
# → returns OutputRuleMetadata with rule_raw and description populated (if disclosed)

# Step 3 – enumerate all rules in an incident including non-disclosed metadata
dfx canister call <rate_limit_canister_id> get_rules_by_incident_id '("<incident_uuid>")' --query
# → returns Vec<OutputRuleMetadata> including non-disclosed rules with structural metadata
```

All three calls are non-replicated queries. `inspect_message` is never invoked. The `RestrictedRead` redaction in `RuleConfidentialityFormatter` hides `rule_raw`/`description` for non-disclosed rules but leaks `rule_id`, `incident_id`, `added_in_version`, and `removed_in_version`, and returns full content for all disclosed rules. [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

**File:** rs/boundary_node/rate_limits/canister/access_control.rs (L16-21)
```rust
#[derive(Clone, PartialEq, Eq)]
pub enum AccessLevel {
    FullAccess,
    FullRead,
    RestrictedRead,
}
```

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L33-68)
```rust
// Inspect the ingress messages in the pre-consensus phase and reject early, if the conditions are not met
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

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L110-146)
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

/// Retrieves a specific rate-limit rule by its ID, applying confidentiality formatting, based on caller's access level and rule's confidentiality status
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

**File:** rs/boundary_node/rate_limits/canister/confidentiality_formatting.rs (L14-43)
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

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L105-148)
```rust
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

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L234-246)
```rust
        let is_authorized_viewer = self.access_resolver.get_access_level()
            == AccessLevel::FullAccess
            || self.access_resolver.get_access_level() == AccessLevel::FullRead;

        if is_authorized_viewer {
            return Ok(output_rule.into());
        }

        // Hide non-disclosed rules from unauthorized viewers.
        let output_rule = self.formatter.format(output_rule);

        Ok(output_rule.into())
    }
```
