Audit Report

## Title
`incident_id` of Undisclosed Security Incidents Exposed to Anonymous Callers via Unenforced `inspect_message` Guard and Incomplete Confidentiality Redaction — (`rs/boundary_node/rate_limits/canister/canister.rs`, `confidentiality_formatting.rs`, `getter.rs`)

## Summary

The `inspect_message` hook intended to gate `get_config` only fires for ingress messages; `#[query]` calls bypass it entirely, so any anonymous caller can invoke `get_config` at zero cost. Even when the handler's own access-control path applies the `ConfigConfidentialityFormatter` for `RestrictedRead` callers, the formatter only nulls `rule_raw` and `description` — it never redacts `incident_id`. As a result, the UUID of every undisclosed security incident, together with the exact timestamp at which the new rate-limit config became active, is observable by any unauthenticated caller.

## Finding Description

**Root cause 1 — `inspect_message` does not cover `#[query]` calls.**

`canister.rs` registers an `#[inspect_message]` hook that traps for any caller without `FullAccess` or `FullRead` when the called method is `get_config`:

```
const REPLICATED_QUERY_METHOD: &str = "get_config";   // line 31
// inspect_message hook, lines 48-55: traps for unauthorized callers
```

However, `get_config` is declared `#[query]` (line 110), which means it is answered by a single replica as a non-replicated query. Per the IC interface specification, `inspect_message` is invoked **only for ingress messages** (update calls and replicated queries submitted through the ingress path). A plain `#[query]` call never triggers `inspect_message`, so the trap at lines 52-54 is never reached for query callers.

**Root cause 2 — `incident_id` is not redacted by `ConfigConfidentialityFormatter`.**

When the caller is not `FullAccess`/`FullRead`, `getter.rs` lines 142-147 apply the formatter:

```rust
Ok(api::ConfigResponse {
    config: self.formatter.format(config).into(),
    ...
})
```

`ConfigConfidentialityFormatter::format` (lines 17-28 of `confidentiality_formatting.rs`) only sets `rule.description = None` and `rule.rule_raw = None` for non-disclosed rules. `incident_id` is never touched. The `OutputRule → api::OutputRule` conversion in `types.rs` lines 78-87 then unconditionally serialises `incident_id` into the wire response:

```rust
api::OutputRule {
    incident_id: value.incident_id.to_string(),   // always present
    rule_raw: value.rule_raw,                      // None for restricted
    description: value.description,               // None for restricted
    ...
}
```

The existing unit test in `getter.rs` lines 358-384 confirms this: for `AccessLevel::RestrictedRead`, the response for a non-disclosed rule (`disclosed_at: None`) still carries `incident_id: incident_id.0.to_string()`.

**Exploit path:**

1. Anonymous caller sends a non-replicated query to `get_config(null)` — `inspect_message` is never invoked.
2. `AccessLevelResolver::get_access_level` returns `RestrictedRead` (lines 38-55 of `access_control.rs`).
3. `ConfigGetter::get` applies the formatter and returns a `ConfigResponse` containing every rule's `incident_id`, `rule_id`, `version`, and `active_since`, with only `rule_raw`/`description` nulled.
4. Caller observes the UUID of every undisclosed security incident and the exact activation timestamp.

## Impact Explanation

The boundary node rate-limit canister is an explicitly in-scope boundary/API component. Its confidentiality model is designed to keep `incident_id` private until `disclose_rules` is called. Leaking the UUID before disclosure constitutes a significant boundary/API security impact: any anonymous party can (a) detect the moment a new security-incident rate-limit is deployed (timing oracle), and (b) obtain the UUID of the undisclosed incident, enabling correlation with public incident trackers or on-chain activity to infer that a security incident is actively being mitigated. This maps to **High ($2,000–$10,000)**: significant boundary/API infrastructure security impact with concrete harm to the confidentiality guarantees of the protocol's incident-response tooling.

## Likelihood Explanation

The attack requires no privileges, no cycles, no authentication, and no special tooling. A single HTTP query to the canister endpoint suffices. The `inspect_message` bypass is a structural property of the IC protocol (not a misconfiguration), so it is unconditionally and repeatably exploitable on mainnet by any anonymous caller.

## Recommendation

1. **Remove the `inspect_message` guard for `get_config`** — it provides no protection for query calls. Enforce access control exclusively inside the handler: if the resolved level is `RestrictedRead` and the intent is to block public access entirely, return an explicit authorization error instead of a redacted response.
2. **Redact `incident_id` in `ConfigConfidentialityFormatter::format`** for rules where `disclosed_at.is_none()`, replacing it with an empty string or a stable opaque placeholder, so the UUID is not observable before disclosure.
3. Apply the same fix to `RuleConfidentialityFormatter` for consistency, as `get_rule_by_id` and `get_rules_by_incident_id` share the same redaction gap.

## Proof of Concept

```bash
# Zero privileges, zero cycles, anonymous caller:
dfx canister call <rate_limit_canister_id> get_config '(null)' --query
# Returns ConfigResponse { version: N, active_since: T, config: { rules: [
#   { rule_id: "...", incident_id: "<UUID-of-undisclosed-incident>",
#     rule_raw: null, description: null }, ... ] } }

# After version increments:
dfx canister call <rate_limit_canister_id> get_config '(opt N)' --query
# Returns incident_id for every newly added non-disclosed rule.
```

The `--query` flag sends a non-replicated query call. `inspect_message` is never invoked. The response includes `incident_id` for every rule regardless of `disclosed_at` status, confirmed by the existing unit test at `getter.rs` lines 358–384.