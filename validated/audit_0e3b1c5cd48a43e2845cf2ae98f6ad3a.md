Audit Report

## Title
Incomplete Redaction in Confidentiality Formatters Leaks Non-Disclosed Rule and Incident UUIDs to Unprivileged Callers - (`rs/boundary_node/rate_limits/canister/confidentiality_formatting.rs`, `rs/boundary_node/rate_limits/canister/getter.rs`)

## Summary
Both `ConfigConfidentialityFormatter` and `RuleConfidentialityFormatter` only null out `rule_raw` and `description` for non-disclosed rules, leaving `rule_id`, `incident_id`, `added_in_version`, and `removed_in_version` intact in responses to `RestrictedRead` callers. Because `inspect_message` is never invoked for non-replicated query calls on the IC, any anonymous principal can call `get_config`, `get_rule_by_id`, and `get_rules_by_incident_id` as queries and harvest the complete set of rule and incident UUIDs — including those of rules that have never been disclosed — along with their full version history metadata.

## Finding Description

**Root cause — formatter does not redact identifiers.**

In `confidentiality_formatting.rs` lines 21–26, `ConfigConfidentialityFormatter::format` iterates over rules and for each with `disclosed_at.is_none()` sets only `rule.description = None` and `rule.rule_raw = None`. The `id` (rule_id) and `incident_id` fields are never cleared:

```rust
config.rules.iter_mut().for_each(|rule| {
    if rule.disclosed_at.is_none() {
        rule.description = None;
        rule.rule_raw = None;
        // rule.id and rule.incident_id remain populated
    }
});
```

`RuleConfidentialityFormatter::format` (lines 36–41) has the identical gap for `OutputRuleMetadata`, additionally leaving `added_in_version` and `removed_in_version` exposed.

**`inspect_message` does not protect query calls.**

`inspect_message` (canister.rs lines 48–67) gates `get_config` when called as a replicated query/ingress update, requiring `FullAccess` or `FullRead`. However, the IC runtime only invokes `inspect_message` for ingress *update* messages. All three read methods are annotated `#[query]` (canister.rs lines 110, 123, 136), so when invoked as non-replicated queries, `inspect_message` is never triggered. The `RestrictedRead` formatter path inside each getter is reached directly by any anonymous caller.

**Exploit flow.**

1. Anonymous caller issues a non-replicated query `get_config(version)` for versions 1 through `current_version`. Each response contains `rule_id` and `incident_id` for every rule — including non-disclosed ones — with only `rule_raw`/`description` nulled.
2. Caller issues `get_rules_by_incident_id(incident_id)` for each harvested incident UUID, obtaining the full rule UUID set plus `added_in_version`/`removed_in_version` for every historical and active rule.
3. Caller issues `get_rule_by_id(rule_id)` for any harvested UUID; the response confirms existence and exposes version metadata even before disclosure.

**Existing test confirms the behavior.**

The unit test in `getter.rs` lines 368–375 explicitly asserts that for a `RestrictedRead` caller, the redacted response for a non-disclosed rule contains non-null `rule_id` and `incident_id`, confirming this is the current observable behavior.

## Impact Explanation
This constitutes a significant boundary/API security impact with concrete harm: the confidentiality invariant of the rate-limit disclosure mechanism is broken. An attacker learns the complete structural topology of all rate-limit rules and incidents — including those never disclosed — their version history, and the precise moment any rule transitions to disclosed status. This enables targeted probing (polling `get_rule_by_id` with known UUIDs to detect disclosure events and immediately read newly-disclosed rule content) and undermines the operational security model that motivates the disclosure mechanism. This maps to the **High** impact category: significant boundary/API infrastructure security impact with concrete harm to the confidentiality guarantees the system is designed to enforce.

## Likelihood Explanation
The attack requires no privileges, no keys, and no social engineering. Any anonymous principal with access to an IC query endpoint can execute it. The `inspect_message` bypass is a structural property of the IC runtime, not a configuration issue. The formatter gap is confirmed by the existing unit test. Likelihood is **high**.

## Recommendation
1. In `ConfigConfidentialityFormatter::format`, for rules where `disclosed_at.is_none()`, also set `rule.id` to a zeroed/placeholder UUID and `rule.incident_id` to a zeroed/placeholder UUID before returning.
2. In `RuleConfidentialityFormatter::format`, apply the same redaction to `id`, `incident_id`, `added_in_version`, and `removed_in_version` for non-disclosed rules.
3. Add an explicit in-method access-level guard in `get_rule_by_id` and `get_rules_by_incident_id` that returns an authorization error for `RestrictedRead` callers attempting to look up non-disclosed entities by UUID directly.
4. Consider whether `get_config` should enforce the same `FullAccess`/`FullRead` restriction for non-replicated query calls via an in-method guard, consistent with the intent expressed in `inspect_message`.

## Proof of Concept
The existing unit test `test_get_config_success` in `getter.rs` lines 358–384 already constitutes a proof of concept. It constructs a `ConfigGetter` with `AccessLevel::RestrictedRead`, calls `getter.get(&Some(1))`, and asserts that the response for the non-disclosed `rule_id_1` contains `rule_id: rule_id_1.0.to_string()` and `incident_id: incident_id.0.to_string()` — both non-empty — while `rule_raw` and `description` are `None`. Running `cargo test -p rate-limits-canister test_get_config_success` reproduces this behavior deterministically without any canister deployment.