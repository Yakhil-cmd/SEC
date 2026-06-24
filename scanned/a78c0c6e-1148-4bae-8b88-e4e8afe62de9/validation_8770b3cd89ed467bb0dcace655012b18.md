### Title
`inspect_message` Caller Guard Bypassed via Direct Query Calls, Leaking Confidential Rate-Limit Rule Metadata - (File: `rs/boundary_node/rate_limits/canister/canister.rs`)

### Summary
The rate-limits canister relies on an `inspect_message` hook to restrict which callers may invoke `get_config`, `get_rule_by_id`, and `get_rules_by_incident_id`. Because all three are annotated `#[query]`, the IC protocol never invokes `inspect_message` for them; any unprivileged ingress sender or canister can call them directly as query calls. The internal `AccessLevelResolver` falls back to `RestrictedRead` for unknown callers, which redacts `rule_raw` and `description` but still returns every non-disclosed rule's `rule_id`, `incident_id`, `added_in_version`, and `removed_in_version` — metadata the canister's own design treats as confidential until explicitly disclosed.

### Finding Description
`inspect_message` is an IC-CDK hook that is invoked only for **ingress update messages** before they reach consensus. It is never called for query calls. [1](#0-0) 

The hook guards three logical paths:

| Method | Allowed callers (per `inspect_message`) |
|---|---|
| `get_config` | `authorized_principal` OR API boundary node |
| `add_config`, `disclose_rules` | `authorized_principal` only |
| everything else | rejected |

All three read methods are declared `#[query]`: [2](#0-1) 

Because they are query endpoints, the IC runtime routes them directly to execution without ever calling `inspect_message`. The constant `REPLICATED_QUERY_METHOD` signals the developers intended `get_config` to be invoked as a replicated query (update call), but the `#[query]` annotation makes it reachable as a plain query call too, silently bypassing the guard. [3](#0-2) 

Inside each method, `AccessLevelResolver::get_access_level()` returns `RestrictedRead` for any caller that is neither the `authorized_principal` nor a registered API boundary node: [4](#0-3) 

`RestrictedRead` causes the confidentiality formatter to null out `rule_raw` and `description` for non-disclosed rules, but the structural metadata is returned verbatim: [5](#0-4) [6](#0-5) 

### Impact Explanation
An unprivileged ingress sender can query `get_config`, `get_rule_by_id`, or `get_rules_by_incident_id` and receive the full list of non-disclosed rule UUIDs, their associated incident UUIDs, and the config versions in which they were added or removed. Rate-limit rules are kept confidential precisely because they describe active security incidents; premature disclosure of their existence and version timeline allows an attacker to:

1. Confirm that a specific incident is being tracked before public disclosure.
2. Correlate incident UUIDs across calls to reconstruct the timeline of security events.
3. Adjust attack traffic to avoid triggering rules whose existence is now known.

### Likelihood Explanation
The attack requires no special privilege, no key material, and no inter-canister coordination. Any user with an IC identity (or even an anonymous principal) can issue a query call to the canister's public endpoint. The canister is deployed on the NNS subnet and its canister ID is publicly known. Exploitation is trivially scriptable.

### Recommendation
1. **Change the three read methods to `#[update]`** (replicated query semantics) so that `inspect_message` is actually invoked for every ingress call. This matches the developer intent expressed by `REPLICATED_QUERY_METHOD`.
2. **Add an explicit caller check inside each method** as a defense-in-depth measure, independent of `inspect_message`, so that inter-canister callers are also subject to the same access control.
3. Alternatively, if `#[query]` semantics are required for latency reasons, remove the `inspect_message` entries for these methods and enforce access control solely through `AccessLevelResolver`, but ensure `RestrictedRead` returns no metadata for non-disclosed rules.

### Proof of Concept
```
# Any principal, including anonymous, can issue:
dfx canister --network ic call <rate-limits-canister-id> get_config '(null)' --query

# Response includes rule_id and incident_id for every non-disclosed rule,
# even though inspect_message would have rejected this caller for an update call.

dfx canister --network ic call <rate-limits-canister-id> get_rule_by_id '("<uuid>")' --query
# Returns rule metadata (incident_id, added_in_version, removed_in_version)
# for a non-disclosed rule — a method inspect_message would reject entirely
# for an update call.
``` [2](#0-1) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L29-31)
```rust
const REGISTRY_CANISTER_METHOD: &str = "get_api_boundary_node_ids";
const UPDATE_METHODS: [&str; 2] = ["add_config", "disclose_rules"];
const REPLICATED_QUERY_METHOD: &str = "get_config";
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

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L130-148)
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
    }
```

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L234-245)
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
```
