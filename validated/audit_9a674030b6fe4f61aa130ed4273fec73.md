### Title
Inconsistent Caller Authorization: `inspect_message` Guards `get_config` as Replicated Query but `get_rule_by_id` and `get_rules_by_incident_id` Are Unguarded Query Methods - (File: rs/boundary_node/rate_limits/canister/canister.rs)

### Summary
The rate-limit canister enforces caller-based access control in `inspect_message` for only one query method (`get_config`) when called as a replicated query (update call), while two other query methods with identical confidentiality semantics — `get_rule_by_id` and `get_rules_by_incident_id` — are declared as plain `#[query]` methods and are entirely absent from the `inspect_message` guard. Any unprivileged ingress caller can invoke these two methods as standard (non-replicated) queries without passing through `inspect_message` at all, bypassing the intended access-control layer. The confidentiality formatting inside the getter logic still runs, but the design intent — that only authorized principals (the `authorized_principal` or registered API boundary nodes) may read rate-limit rule data — is broken for these two endpoints.

### Finding Description
The `inspect_message` hook in `rs/boundary_node/rate_limits/canister/canister.rs` defines two allowlists:

- `REPLICATED_QUERY_METHOD = "get_config"` — accepted only for `FullAccess` or `FullRead` callers when invoked as a replicated query (update call).
- `UPDATE_METHODS = ["add_config", "disclose_rules"]` — accepted only for `FullAccess` callers.

All other method names are trapped with `"message_inspection_failed: method call is prohibited in the current context"`.

However, `inspect_message` is **only invoked for ingress update calls** (replicated queries and update calls). It is **never invoked for non-replicated query calls**. The two methods `get_rule_by_id` and `get_rules_by_incident_id` are annotated `#[query]`, meaning they are served as non-replicated queries by boundary nodes and never pass through `inspect_message`. Any caller — including anonymous — can call them freely.

The confidentiality formatter (`RuleConfidentialityFormatter`) inside `RuleGetter` and `IncidentGetter` does redact non-disclosed `rule_raw` and `description` fields for `RestrictedRead` callers. However, the design intent expressed by the `inspect_message` guard is that even `RestrictedRead` access to these endpoints should be restricted to registered API boundary node principals. The `get_config` method is protected at the `inspect_message` level (when called as a replicated query), but `get_rule_by_id` and `get_rules_by_incident_id` have no equivalent gate.

This is the direct IC analog of the reported Solidity bug: the protocol uses a caller-identity access-control mechanism (`inspect_message`) extensively, but not everywhere — specifically, the two query getter methods are missing the guard that the analogous `get_config` method has.

### Impact Explanation
**Impact: Low-to-Medium.**

The confidentiality formatter still redacts undisclosed `rule_raw` and `description` fields for unauthorized callers. However:

1. **Metadata leakage**: Even for undisclosed rules, the `rule_id`, `incident_id`, `added_in_version`, `removed_in_version`, and `disclosed_at` fields are returned unredacted to any anonymous caller via `get_rule_by_id` and `get_rules_by_incident_id`. This leaks the existence, timing, and versioning of security incidents before they are publicly disclosed.
2. **Design intent violation**: The `inspect_message` guard was explicitly designed to prevent unauthorized callers from reading any rate-limit configuration data. This intent is fully bypassed for two of the three read endpoints.
3. **Enumeration**: An attacker who knows or guesses a `rule_id` or `incident_id` UUID can confirm its existence and learn its metadata (version ranges, disclosure timestamp) without being an authorized principal or API boundary node.

### Likelihood Explanation
**Likelihood: High.**

The two methods `get_rule_by_id` and `get_rules_by_incident_id` are declared as standard `#[query]` in the Candid interface and are publicly callable by any ingress sender. No special knowledge or privilege is required. The IC HTTP query endpoint is publicly accessible at any boundary node. This is certain to occur as the code stands.

### Recommendation
Either:
1. Add `"get_rule_by_id"` and `"get_rules_by_incident_id"` to the `inspect_message` guard (as replicated query methods, analogous to `get_config`), so that unauthorized callers are rejected before execution; or
2. Add an explicit runtime authorization check at the top of `get_rule_by_id` and `get_rules_by_incident_id` that traps (panics) if the caller does not have at least `FullRead` access — mirroring the intent of the `inspect_message` guard for `get_config`.

Option 1 requires callers to invoke these methods as replicated queries (update calls), which is consistent with the existing `get_config` pattern. Option 2 preserves the `#[query]` annotation but adds an in-method guard.

### Proof of Concept

The `inspect_message` function only lists `get_config` as a protected query method: [1](#0-0) 

The two unguarded query methods are declared as plain `#[query]` and are reachable by any caller: [2](#0-1) 

The Candid interface confirms both are exposed as `query` (non-replicated): [3](#0-2) 

The `AccessLevelResolver` correctly computes `RestrictedRead` for anonymous callers, but the getter still returns metadata fields (rule/incident IDs, version ranges, disclosure timestamps) even for `RestrictedRead`: [4](#0-3) 

An attacker sends a standard IC query call (no authentication required) to `get_rule_by_id` or `get_rules_by_incident_id` with a known or guessed UUID. The call bypasses `inspect_message` entirely (since `inspect_message` is not invoked for non-replicated queries), reaches the getter, and returns metadata about undisclosed security incidents. The `inspect_message` guard that was intended to block this is never triggered for `#[query]` methods. [5](#0-4)

### Citations

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L30-67)
```rust
const UPDATE_METHODS: [&str; 2] = ["add_config", "disclose_rules"];
const REPLICATED_QUERY_METHOD: &str = "get_config";

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
```

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L122-146)
```rust
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

**File:** rs/boundary_node/rate_limits/canister/interface.did (L151-155)
```text
  // Fetch the rule with metadata by its ID
  get_rule_by_id: (RuleId) -> (GetRuleByIdResponse) query;

  // Fetch all rules with metadata related to an ID of the incident
  get_rules_by_incident_id: (IncidentId) -> (GetRulesByIncidentIdResponse) query;
```

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L215-246)
```rust
    fn get(&self, rule_id: &Self::Input) -> Result<Self::Output, Self::Error> {
        let rule_id = RuleId::try_from(rule_id.clone())
            .map_err(|_| GetEntityError::InvalidUuidFormat(rule_id.clone()))?;

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
