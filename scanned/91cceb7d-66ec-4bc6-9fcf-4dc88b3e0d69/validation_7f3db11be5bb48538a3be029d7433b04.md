The code is clear enough to make a determination. Let me verify the exact behavior of the formatter and the `inspect_message` guard.

The vulnerability is **real and concrete**. Here is the analysis:

---

**Two independent weaknesses combine into a working exploit:**

**1. `inspect_message` does not protect query calls.**

In `canister.rs`, the `inspect_message` hook restricts `get_config` to `FullAccess`/`FullRead` callers: [1](#0-0) 

However, on the Internet Computer, `inspect_message` is only invoked for **ingress (update) messages**. Regular `#[query]` calls bypass it entirely. Since `get_config`, `get_rule_by_id`, and `get_rules_by_incident_id` are all annotated `#[query]`, any anonymous caller can invoke them as query calls without triggering `inspect_message`.

**2. The confidentiality formatter does not redact `rule_id` or `incident_id`.**

`ConfigConfidentialityFormatter::format` only nulls out `description` and `rule_raw` for non-disclosed rules, leaving `rule_id` and `incident_id` fully populated: [2](#0-1) 

This is confirmed by the existing test, which explicitly asserts that a `RestrictedRead` caller receives `rule_id` and `incident_id` for a non-disclosed rule: [3](#0-2) 

Similarly, `RuleConfidentialityFormatter` also leaves `rule_id`, `incident_id`, `added_in_version`, and `removed_in_version` intact: [4](#0-3) 

---

### Title
Unprivileged callers enumerate non-disclosed rule and incident identifiers via query-call bypass of `inspect_message` — (`rs/boundary_node/rate_limits/canister/getter.rs`, `canister.rs`, `confidentiality_formatting.rs`)

### Summary
The `inspect_message` guard that restricts `get_config` to privileged callers is ineffective for IC query calls. Combined with a confidentiality formatter that redacts only `rule_raw`/`description` but leaves `rule_id` and `incident_id` exposed, any anonymous caller can enumerate the complete set of non-disclosed rule and incident identifiers across all historical config versions.

### Finding Description
`canister.rs` registers `get_config` as `REPLICATED_QUERY_METHOD` and gates it in `inspect_message` to `FullAccess`/`FullRead` callers only. However, `inspect_message` is an IC system hook that fires exclusively for **ingress (update) messages**. All three read methods (`get_config`, `get_rule_by_id`, `get_rules_by_incident_id`) are `#[query]` methods. Query calls on the IC are handled by a single replica without going through consensus and are never subject to `inspect_message`. The guard is therefore a no-op for query callers.

Once an anonymous caller reaches `ConfigGetter::get`, the `ConfigConfidentialityFormatter` is applied for `RestrictedRead` access. It sets `is_redacted = true` and nulls `rule_raw`/`description` for non-disclosed rules, but the `OutputRule` struct's `rule_id` and `incident_id` fields are left intact. The same applies to `RuleConfidentialityFormatter` used in `IncidentGetter::get` and `RuleGetter::get`, which additionally leaves `added_in_version` and `removed_in_version` exposed.

### Impact Explanation
An anonymous attacker can:
1. Call `get_config(None)` as a query call to get the latest version number and all `rule_id`/`incident_id` pairs (including non-disclosed rules).
2. Iterate versions 1..=current to collect the full historical set.
3. Call `get_rules_by_incident_id(incident_id)` for each collected `incident_id` to obtain `added_in_version` and `removed_in_version` for every rule ever created.

This reveals the complete structural topology of all rate-limit rules and incidents — how many rules exist, which incidents they belong to, when they were added/removed — enabling targeted timing-based inference of rule content and probing of boundary node enforcement behavior.

### Likelihood Explanation
The attack requires no credentials, no special tooling, and no on-chain state. Any IC query call from an anonymous principal suffices. The `inspect_message` bypass is a well-known IC property; the formatter gap is directly visible in the code and confirmed by the existing test suite.

### Recommendation
1. **Move access control into the query handlers themselves**, not only into `inspect_message`. Check `access_resolver.get_access_level()` at the top of each query handler and return an error for `RestrictedRead` callers on sensitive methods, or remove the `inspect_message` guard and rely solely on in-handler checks.
2. **Redact `rule_id` and `incident_id` for non-disclosed rules** in `ConfigConfidentialityFormatter` and `RuleConfidentialityFormatter`, or filter out non-disclosed rules entirely from the response for `RestrictedRead` callers.
3. Alternatively, if `get_config` is intended to be replicated-query only, enforce this at the canister level and document that regular query access is not supported.

### Proof of Concept
```rust
// In a unit test or integration test:
// 1. Add an undisclosed rule (disclosed_at: None)
// 2. Call get_config as RestrictedRead (anonymous principal)
// 3. Assert rule_id and incident_id are non-null in the redacted response
// (This is already proven by the existing test at getter.rs:358-384)

let response = getter_unauthorized.get(&Some(1)).unwrap();
let rule = &response.config.rules[0]; // non-disclosed rule
assert!(rule.rule_id != "");       // rule_id IS exposed — confirmed
assert!(rule.incident_id != "");   // incident_id IS exposed — confirmed
assert!(rule.rule_raw.is_none());  // only content is redacted
```

The existing test at `getter.rs:358–384` already asserts this exact behavior, confirming the leak is intentional in the formatter but unguarded at the transport layer.

### Citations

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

**File:** rs/boundary_node/rate_limits/canister/confidentiality_formatting.rs (L31-42)
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
```

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L368-384)
```rust
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
