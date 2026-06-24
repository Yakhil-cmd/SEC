The code is now fully clear. Let me trace the exact execution path.

**Step 1: Can a `RestrictedRead` caller reach `get_config` and `get_rules_by_incident_id`?**

The `inspect_message` hook in `canister.rs` guards `get_config` (as a replicated query/update call) and traps all other update methods for unprivileged callers. However, `inspect_message` is **only invoked for ingress/update messages** — it is never called for regular (non-replicated) query calls on the IC.

`get_config`, `get_rule_by_id`, and `get_rules_by_incident_id` are all annotated `#[query]`. When called as regular query calls, `inspect_message` is bypassed entirely. There is no other access-control guard inside these functions — the only protection is the confidentiality formatter.

**Step 2: What does the formatter actually redact?**

`ConfigConfidentialityFormatter::format` (lines 17–28 of `confidentiality_formatting.rs`) only nulls out `rule_raw` and `description` for non-disclosed rules. It leaves `rule_id` and `incident_id` intact. The existing test at `getter.rs` lines 369–374 explicitly confirms this: a `RestrictedRead` response still contains populated `rule_id` and `incident_id` for a non-disclosed rule.

`RuleConfidentialityFormatter::format` (lines 34–42) does the same: only `rule_raw` and `description` are redacted; `rule_id`, `incident_id`, `added_in_version`, and `removed_in_version` are all returned.

**Step 3: The enumeration chain**

An anonymous caller can:
1. Call `get_config(None)` as a query → learns `current_version`
2. Call `get_config(Some(v))` for v = 1..current_version → collects every `rule_id` and `incident_id` across all historical configs, including non-disclosed rules
3. Call `get_rules_by_incident_id(incident_id)` for each collected `incident_id` → gets `added_in_version` and `removed_in_version` for every rule in that incident

**Conclusion:**

The precondition stated in the question is confirmed by the production code and its own tests. The `inspect_message` guard does not protect query-path calls, and the formatter intentionally preserves identifiers. The full structural topology (all rule UUIDs, incident groupings, version lifetimes) of non-disclosed rules is reachable by any unprivileged caller.

---

### Title
Information Disclosure of Non-Disclosed Rule Identifiers via Unguarded Query Calls — (`rs/boundary_node/rate_limits/canister/getter.rs`, `canister.rs`, `confidentiality_formatting.rs`)

### Summary
An unprivileged (anonymous / `RestrictedRead`) caller can enumerate the complete structural topology of all rate-limit rules and incidents — including non-disclosed ones — by calling `get_config` and `get_rules_by_incident_id` as regular IC query calls, which bypass the `inspect_message` guard entirely.

### Finding Description
The `inspect_message` hook in `canister.rs` is the only mechanism that restricts access to `get_config` for unprivileged callers. However, `inspect_message` is only invoked for **ingress (update) messages**; it is never called for regular query calls. All three read methods (`get_config`, `get_rule_by_id`, `get_rules_by_incident_id`) are `#[query]` methods with no in-function access control beyond the confidentiality formatter.

The confidentiality formatter (`ConfigConfidentialityFormatter` and `RuleConfidentialityFormatter`) only redacts `rule_raw` and `description` for non-disclosed rules. The fields `rule_id`, `incident_id`, `added_in_version`, and `removed_in_version` are always returned, regardless of disclosure status or caller access level. [1](#0-0) [2](#0-1) [3](#0-2) 

### Impact Explanation
An attacker learns:
- The UUID of every rate-limit rule ever added, including non-disclosed (active security) rules
- The incident grouping of every rule
- The exact version range (`added_in_version`, `removed_in_version`) during which each rule was active

This exposes the complete structural topology of the rate-limit system, enabling targeted probing (e.g., timing attacks against boundary nodes to infer rule content by correlating known rule lifetimes with observed enforcement behavior). [4](#0-3) 

### Likelihood Explanation
The attack requires no privileges, no keys, and no special tooling — only the ability to send IC query calls, which is available to any internet user via the public IC API. The enumeration is fully deterministic and requires at most `current_version + N` query calls.

### Recommendation
1. In `ConfigConfidentialityFormatter::format` and `RuleConfidentialityFormatter::format`, also null out `rule_id` and `incident_id` (replacing them with placeholder/empty values) for non-disclosed rules when the caller is `RestrictedRead`.
2. Alternatively, filter non-disclosed rules out of the response entirely for `RestrictedRead` callers, rather than including them with partial redaction.
3. Add an explicit access-level check inside `get_config`, `get_rule_by_id`, and `get_rules_by_incident_id` (not only in `inspect_message`) so that the guard is effective for both query and update call paths.

### Proof of Concept
The existing unit test at `getter.rs` lines 358–384 already proves the precondition: a `RestrictedRead` caller receives a response where `rule_id` and `incident_id` are non-null for a non-disclosed rule. The only additional step needed to demonstrate the full exploit is to call `get_rules_by_incident_id` with the leaked `incident_id` and observe that `added_in_version` and `removed_in_version` are also returned. [5](#0-4) [6](#0-5)

### Citations

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

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L113-121)
```rust
            let output_rule = OutputRule {
                id: *rule_id,
                incident_id: rule.incident_id,
                rule_raw: Some(rule.rule_raw),
                description: Some(rule.description),
                disclosed_at: rule.disclosed_at,
            };

            rules.push(output_rule);
```

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L176-199)
```rust
        for rule_id in stored_incident.rule_ids.into_iter() {
            let stored_rule = self.canister_api.get_rule(&rule_id).ok_or_else(|| {
                // This error should never happen, it means that the stored data is inconsistent.
                GetEntityError::Internal(anyhow::anyhow!("Rule with id={rule_id} not found"))
            })?;

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
