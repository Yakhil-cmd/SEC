The code confirms all the key claims. Let me verify the `inspect_message` behavior for query methods specifically.

Audit Report

## Title
Incomplete Redaction in `RuleConfidentialityFormatter` Leaks `incident_id` and Version Metadata for Undisclosed Rules to Any Anonymous Caller — (`rs/boundary_node/rate_limits/canister/confidentiality_formatting.rs`, `getter.rs`)

## Summary

`RuleConfidentialityFormatter::format` only nullifies `rule_raw` and `description` for undisclosed rules, leaving `incident_id`, `added_in_version`, and `removed_in_version` in the response. Because `get_rule_by_id` and `get_rules_by_incident_id` are `#[query]` methods, `inspect_message` never fires for them, so any anonymous principal can call them freely. The rule IDs and incident IDs needed to drive these calls are already returned by `get_config` (also a query) for `RestrictedRead` callers. The result is a fully reachable, zero-privilege metadata disclosure for every undisclosed rate-limit rule.

## Finding Description

**Root cause — `RuleConfidentialityFormatter::format` (`confidentiality_formatting.rs` L34–42):**
```rust
fn format(&self, rule: OutputRuleMetadata) -> OutputRuleMetadata {
    let mut rule = rule;
    if rule.disclosed_at.is_none() {
        rule.description = None;
        rule.rule_raw = None;   // only these two fields are cleared
    }
    rule  // incident_id, added_in_version, removed_in_version returned as-is
}
```
`incident_id`, `added_in_version`, and `removed_in_version` are never set to `None` for undisclosed rules.

**`RuleGetter::get` and `IncidentGetter::get` populate all fields before calling the formatter (`getter.rs` L224–243, L182–197):**
Both paths build a fully-populated `OutputRuleMetadata` (including `incident_id`, `added_in_version`, `removed_in_version`) and then call `self.formatter.format(output_rule)`, which only strips `rule_raw`/`description`. The remaining metadata fields survive into the response.

**`inspect_message` does not guard query calls (`canister.rs` L34–67):**
`inspect_message` is an IC ingress-message hook; it is never invoked for `#[query]` calls. `get_rule_by_id` (L123) and `get_rules_by_incident_id` (L136) are both declared `#[query]`. Any anonymous principal can call them directly without triggering the hook. The `else` branch that traps "all other calls" only applies to ingress (update) messages.

**`get_config` provides the seed IDs (`getter.rs` L358–384 test, `canister.rs` L111):**
For a `RestrictedRead` caller (the level assigned to every anonymous principal by `AccessLevelResolver`, `access_control.rs` L38–55), `get_config` returns every rule's `rule_id` and `incident_id` in plaintext. This gives the attacker the complete set of identifiers needed to probe `get_rule_by_id` and `get_rules_by_incident_id`.

**Unit test confirms the leak (`getter.rs` L433–446):**
The existing test explicitly asserts that a `RestrictedRead` caller receives `incident_id`, `added_in_version: 1`, and `removed_in_version: Some(3)` for an undisclosed rule — confirming the behavior is present in the committed code.

## Impact Explanation

The rate-limit canister is part of the boundary node infrastructure, which is explicitly in scope. The confidentiality property of undisclosed rules is the system's stated security invariant: operators withhold `rule_raw`/`description` to prevent attackers from learning what traffic patterns are being suppressed during an active security incident. The metadata leak breaks a meaningful portion of that invariant:

- An attacker learns the **existence** of every undisclosed rule (count, IDs).
- Via `incident_id`, they learn **which rules belong to the same security incident**, revealing the scope of an active response.
- Via `added_in_version`/`removed_in_version`, they learn the **exact config versions** at which each rule was introduced or retired, giving timing information about when incidents began and ended.
- Via `get_rules_by_incident_id`, they can enumerate the **full rule set per incident** with version history in a single call.

This constitutes a significant boundary/API infrastructure security impact: the confidentiality mechanism protecting active incident response is partially defeated, with concrete information disclosure to any unauthenticated caller. This matches the **High** impact class: *Significant boundary/API security impact with concrete user or protocol harm*.

## Likelihood Explanation

Exploitability requires no authentication, no special tooling, no timing dependency, and no privileged position. Any caller with network access to the canister executes the full three-step sequence using standard IC query calls (`dfx canister call --query` or any agent library). The attack is repeatable, deterministic, and leaves no trace distinguishable from normal read traffic.

## Recommendation

`RuleConfidentialityFormatter::format` must redact all identifying and timing metadata for undisclosed rules when the caller is not `FullAccess` or `FullRead`. The corrected invariant should be: if `disclosed_at.is_none()`, set `incident_id`, `added_in_version`, and `removed_in_version` to `None` (or return a `NotFound`-equivalent opaque error so the rule's existence is not confirmed). The same fix must be applied symmetrically in `ConfigConfidentialityFormatter::format` for the `OutputRule` entries returned by `get_config`. After the fix, the unit test at `getter.rs` L433–446 must be updated to assert `incident_id: None`, `added_in_version: 0` (or a sentinel), and `removed_in_version: None` for undisclosed rules under `RestrictedRead`.

## Proof of Concept

```bash
# Step 1 — obtain all rule_ids and incident_ids (anonymous query, no auth)
dfx canister call <rate_limit_canister> get_config '(null)' --query
# Returns: rule_id and incident_id in plaintext for every rule, including undisclosed ones.

# Step 2 — probe version metadata per rule
dfx canister call <rate_limit_canister> get_rule_by_id '("<rule_id_from_step1>")' --query
# Returns for an undisclosed rule:
# { rule_id = "<uuid>"; incident_id = "<uuid>"; rule_raw = null; description = null;
#   disclosed_at = null; added_in_version = N; removed_in_version = opt M }

# Step 3 — enumerate all rules per incident with full version history
dfx canister call <rate_limit_canister> get_rules_by_incident_id '("<incident_id_from_step1>")' --query
# Returns all rules for the incident, each with incident_id, added_in_version, removed_in_version.
```

**Invariant regression test** (should be added and must pass after the fix):
```rust
let response = getter_unauthorized.get(&rule_id.0.to_string()).unwrap();
assert_eq!(response.incident_id, "");   // or assert field is None/opaque
assert_eq!(response.added_in_version, 0);
assert_eq!(response.removed_in_version, None);
assert_eq!(response.rule_raw, None);
assert_eq!(response.description, None);
```