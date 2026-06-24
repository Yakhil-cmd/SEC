Let me check how rule IDs are generated and whether they are predictable.

I have all the information needed. Let me analyze the complete picture.

### Title
Undisclosed Rate-Limit Rules Leak `incident_id` and Version Metadata to Any Caller — (`rs/boundary_node/rate_limits/canister/getter.rs`, `confidentiality_formatting.rs`)

### Summary

`RuleConfidentialityFormatter::format` only redacts `rule_raw` and `description` for undisclosed rules. It leaves `incident_id`, `added_in_version`, and `removed_in_version` in the response. Because `get_rule_by_id` and `get_rules_by_incident_id` are `#[query]` methods, any anonymous caller can invoke them directly, bypassing the `inspect_message` guard entirely. Rule IDs needed to probe `get_rule_by_id` are already obtainable from `get_config` (also a query). The result is a concrete, reachable metadata disclosure for every undisclosed rule.

### Finding Description

**`RuleConfidentialityFormatter::format` incomplete redaction:** [1](#0-0) 

Only `rule_raw` and `description` are set to `None`. The fields `incident_id`, `added_in_version`, `removed_in_version`, and `disclosed_at` (which is `None`, confirming undisclosed status) are returned as-is.

**`RuleGetter::get` builds the full `OutputRuleMetadata` before redaction:** [2](#0-1) 

All fields including `incident_id`, `added_in_version`, `removed_in_version` are populated, then only `rule_raw`/`description` are stripped by the formatter.

**The same leak exists in `IncidentGetter::get`:** [3](#0-2) 

**`inspect_message` does not protect query calls:** [4](#0-3) 

`inspect_message` only fires for ingress (update) messages. `get_rule_by_id` and `get_rules_by_incident_id` are `#[query]` methods — they are never subject to this hook. Any anonymous principal can call them.

**`get_config` (also a query) exposes all rule IDs and incident IDs:** [5](#0-4) 

For `RestrictedRead` callers, `get_config` returns every rule's `rule_id` and `incident_id` in plaintext (only `rule_raw`/`description` redacted). This gives the attacker the full set of rule IDs needed to probe `get_rule_by_id`.

**Test confirms the leak explicitly:** [6](#0-5) 

The test asserts that a `RestrictedRead` caller receives `incident_id`, `added_in_version: 1`, `removed_in_version: Some(3)` for an undisclosed rule.

### Impact Explanation

Any anonymous caller can:
1. Call `get_config` as a query → obtain all `rule_id` and `incident_id` values for every rule (including undisclosed ones).
2. Call `get_rule_by_id` per rule ID → obtain `added_in_version` and `removed_in_version` for each undisclosed rule.
3. Call `get_rules_by_incident_id` per incident UUID → enumerate all rules grouped by incident, with full version history.

This reveals: (a) the existence of undisclosed rules, (b) which rules belong to the same security incident, (c) exactly which config versions introduced or removed each rule. An attacker learns the scope and timing of active incidents without any privileged access.

**Correction to the question's premise**: Rule IDs are generated via `getrandom::getrandom` (cryptographically secure), not a predictable ChaCha20 seed. [7](#0-6) 

The "ChaCha20 seed prediction" step is unnecessary — rule IDs are already exposed by `get_config`.

**Limitation on the "targeted evasion" claim**: Without `rule_raw`, the attacker does not learn what traffic patterns are being rate-limited. The practical impact is metadata disclosure (incident groupings, version history), not direct rule-content evasion.

### Likelihood Explanation

Exploitability is trivial: no authentication, no special tooling, no timing dependency. Any caller with network access to the canister can execute the full call sequence using standard IC query calls.

### Recommendation

`RuleConfidentialityFormatter::format` must also redact `incident_id`, `added_in_version`, and `removed_in_version` for undisclosed rules, or return a `NotFound`-equivalent opaque response. The invariant should be: if `disclosed_at.is_none()` and the caller is not `FullAccess`/`FullRead`, return only `rule_id` with all other fields `None` (or reject the lookup entirely).

### Proof of Concept

```
// Step 1: get all rule IDs and incident IDs (anonymous query call)
dfx canister call <rate_limit_canister> get_config '(null)' --query

// Step 2: for each rule_id returned, probe version metadata
dfx canister call <rate_limit_canister> get_rule_by_id '("<rule_id>")' --query
// Response for undisclosed rule:
// { rule_id = "<uuid>"; incident_id = "<uuid>"; rule_raw = null; description = null;
//   disclosed_at = null; added_in_version = 3; removed_in_version = null }

// Step 3: enumerate all rules per incident
dfx canister call <rate_limit_canister> get_rules_by_incident_id '("<incident_id>")' --query
```

The invariant test would be: assert that `get_rule_by_id` for an undisclosed rule returns a response where `incident_id`, `added_in_version`, and `removed_in_version` are all `None` (or the call returns `NotFound`) for any `RestrictedRead` caller.

### Citations

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

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L182-197)
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
```

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L224-243)
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
```

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L433-446)
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
```

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L34-67)
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
```

**File:** rs/boundary_node/rate_limits/canister/add_config.rs (L186-193)
```rust
fn generate_random_uuid() -> Result<Uuid, anyhow::Error> {
    let mut buf = [0_u8; 16];
    getrandom::getrandom(&mut buf)
        .map_err(|e| anyhow::anyhow!(e))
        .context("Failed to generate random bytes")?;
    let uuid = Uuid::from_slice(&buf).context("Failed to create UUID from bytes")?;
    Ok(uuid)
}
```
