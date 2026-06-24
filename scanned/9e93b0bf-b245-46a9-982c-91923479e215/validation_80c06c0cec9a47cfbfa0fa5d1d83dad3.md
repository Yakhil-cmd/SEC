### Title
`inspect_message` Guard Bypassed via Query Calls, Leaking Undisclosed Rule Metadata — (`rs/boundary_node/rate_limits/canister/canister.rs`)

---

### Summary

The `inspect_message` hook in the rate-limits canister is intended to restrict access to `get_config`, `get_rule_by_id`, and `get_rules_by_incident_id` for unprivileged callers. However, on the Internet Computer, `inspect_message` **only fires for ingress update messages**, never for query calls. Since all three read methods are declared `#[query]`, any anonymous caller can invoke them as regular query calls, completely bypassing the guard. For undisclosed rules, the response still exposes `rule_id`, `incident_id`, `added_in_version`, and `removed_in_version` — only `rule_raw` and `description` are redacted.

---

### Finding Description

**Root cause — `inspect_message` does not apply to query calls:**

`inspect_message` is registered as a pre-consensus hook that only intercepts ingress *update* messages. The IC protocol never invokes it for query calls. [1](#0-0) 

The three read methods are all `#[query]`: [2](#0-1) [3](#0-2) 

**What a `RestrictedRead` caller receives for an undisclosed rule:**

`RuleGetter::get()` returns `Ok(metadata)` (confirming existence) and populates `rule_id`, `incident_id`, `added_in_version`, and `removed_in_version` before the formatter runs: [4](#0-3) 

`RuleConfidentialityFormatter::format()` only nulls out `rule_raw` and `description`; it leaves `rule_id`, `incident_id`, and version fields intact: [5](#0-4) 

The same pattern applies in `ConfigGetter` — undisclosed rules are included in the response with their `rule_id` and `incident_id` visible: [6](#0-5) 

This is confirmed by the existing unit test, which explicitly asserts that a `RestrictedRead` caller receives `rule_id` and `incident_id` for an undisclosed rule: [7](#0-6) 

---

### Impact Explanation

An unprivileged attacker can:

1. Call `get_config` as a query → enumerate **all** `rule_id`s and `incident_id`s in the canister, including those of undisclosed rules, along with `added_in_version` / `removed_in_version`.
2. Call `get_rule_by_id(uuid)` as a query → receive `Ok(metadata)` vs `Err(NotFound)`, confirming or denying the existence of any specific rule.
3. Call `get_rules_by_incident_id(uuid)` as a query → enumerate all `rule_id`s associated with a known `incident_id`.

The attacker **cannot** recover `rule_raw` or `description` (the actual rate-limiting payload), so they cannot directly read which canister endpoint is targeted. The concrete impact is **metadata disclosure**: confirmation of undisclosed rule existence, their incident groupings, and the config versions in which they were introduced or removed. This can reveal the timing and scope of an ongoing security response before public disclosure.

---

### Likelihood Explanation

The bypass requires no special privileges, no key material, and no guessing — `get_config` as a query call returns all rule UUIDs directly. The attack is trivially reproducible with a single `dfx canister call --query` invocation against the deployed canister.

---

### Recommendation

1. **Change the read methods to `#[update]`** (replicated queries) so that `inspect_message` fires for them, or
2. **Enforce access control inside each query handler** using the same `AccessLevelResolver` logic already present, and return `Err(NotFound)` (rather than a redacted `Ok`) for undisclosed rules when the caller has `RestrictedRead` access — this prevents existence confirmation.
3. For `get_config`, do not include undisclosed rules in the response at all for `RestrictedRead` callers, rather than including them with redacted fields.

---

### Proof of Concept

```
# Step 1: enumerate all rule_ids and incident_ids (including undisclosed)
dfx canister call --query <rate_limit_canister_id> get_config '(null)'
# → returns all rules with rule_id + incident_id visible, rule_raw/description = null for undisclosed

# Step 2: confirm existence of a specific undisclosed rule
dfx canister call --query <rate_limit_canister_id> get_rule_by_id '("<uuid-from-step-1>")'
# → returns Ok(OutputRuleMetadata { rule_id, incident_id, added_in_version, ... }) not NotFound

# Step 3: enumerate all rules under an incident
dfx canister call --query <rate_limit_canister_id> get_rules_by_incident_id '("<incident_uuid>")'
# → returns all rule_ids for that incident with metadata visible
```

A unit test asserting that `RuleGetter::get()` with `AccessLevel::RestrictedRead` returns `Err(NotFound)` for an undisclosed rule would fail against the current implementation, confirming the invariant violation. [8](#0-7)

### Citations

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

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L110-133)
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
```

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

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L142-148)
```rust
        // Hide non-disclosed rules from unauthorized viewers.
        Ok(api::ConfigResponse {
            version,
            active_since: stored_config.active_since,
            config: self.formatter.format(config).into(),
        })
    }
```

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L219-232)
```rust
        let stored_rule = self
            .canister_api
            .get_rule(&rule_id)
            .ok_or_else(|| GetEntityError::NotFound(rule_id.0.to_string()))?;

        let output_rule = OutputRuleMetadata {
            id: rule_id,
            incident_id: stored_rule.incident_id,
            rule_raw: Some(stored_rule.rule_raw),
            description: Some(stored_rule.description),
            disclosed_at: stored_rule.disclosed_at,
            added_in_version: stored_rule.added_in_version,
            removed_in_version: stored_rule.removed_in_version,
        };
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

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L563-586)
```rust
    #[test]
    fn test_get_not_found_fails() {
        // Arrange
        let canister_state = CanisterState::from_static(); // empty state
        let rule_id = "f63c821c-9320-476a-bc89-94cb99d04639".to_string();
        let incident_id = "f63c821c-9320-476a-bc89-94cb99d04639".to_string();

        let rule_getter = RuleGetter::new(
            canister_state.clone(),
            RuleConfidentialityFormatter,
            create_mock_access_resolver(AccessLevel::RestrictedRead),
        );
        let incident_getter = IncidentGetter::new(
            canister_state,
            RuleConfidentialityFormatter,
            create_mock_access_resolver(AccessLevel::RestrictedRead),
        );

        // Act & assert
        let error = rule_getter.get(&rule_id).unwrap_err();
        assert!(matches!(error, GetEntityError::NotFound(id) if id == rule_id));
        let error = incident_getter.get(&incident_id).unwrap_err();
        assert!(matches!(error, GetEntityError::NotFound(id) if id == incident_id));
    }
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
