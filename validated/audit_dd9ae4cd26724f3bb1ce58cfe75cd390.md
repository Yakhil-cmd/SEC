The code is clear enough to make a determination. Let me verify the `OutputRule` API type to confirm what fields are exposed.

### Title
Unprivileged Callers Bypass `inspect_message` Authorization for `get_config` via Non-Replicated Query, Leaking Confidential Incident and Rule UUIDs — (`rs/boundary_node/rate_limits/canister/canister.rs`)

---

### Summary

The `inspect_message` hook in `canister.rs` is intended to gate access to `get_config` so that only `FullAccess` or `FullRead` callers (authorized principal or API boundary nodes) can invoke it. However, `inspect_message` on ICP only fires for **ingress update messages** — it does not fire for non-replicated query calls. Since `get_config` is declared as a `#[query]` method, any anonymous caller can invoke it as a non-replicated query, bypassing `inspect_message` entirely. The `get_config` handler itself applies no hard authorization rejection for `RestrictedRead` callers; it only invokes `ConfigConfidentialityFormatter`, which redacts `rule_raw` and `description` but leaves `rule_id` and `incident_id` fully exposed for every rule — including non-disclosed confidential ones.

---

### Finding Description

**Entrypoint:** Any unprivileged caller (including anonymous principal) sends a non-replicated query call to `get_config`.

**Step 1 — `inspect_message` is skipped.**

`inspect_message` is registered at line 34–68 of `canister.rs` and checks that callers of `get_config` (named `REPLICATED_QUERY_METHOD`) have `full_access` or `full_read_access`, trapping otherwise: [1](#0-0) 

Per the ICP protocol specification, `inspect_message` is invoked only for ingress **update** messages before they enter consensus. Non-replicated query calls bypass this hook entirely. Since `get_config` is declared `#[query]` and published in the Candid interface as `query`: [2](#0-1) 

...any caller can invoke it as a non-replicated query, and `inspect_message` never fires.

**Step 2 — `get_config` handler runs without authorization rejection.**

The handler resolves the caller's access level via `AccessLevelResolver`. An anonymous principal is neither the authorized principal nor an API boundary node, so it receives `AccessLevel::RestrictedRead`: [3](#0-2) 

The handler does not reject `RestrictedRead` callers. It proceeds to call `ConfigGetter::get()`: [4](#0-3) 

**Step 3 — `ConfigGetter` applies the formatter but does not redact `rule_id` or `incident_id`.**

For `RestrictedRead` callers, `ConfigGetter::get()` applies `ConfigConfidentialityFormatter::format()`: [5](#0-4) 

`ConfigConfidentialityFormatter::format()` only nulls out `rule_raw` and `description` for non-disclosed rules. `rule_id` and `incident_id` are unconditionally preserved: [6](#0-5) 

The `OutputRule` struct confirms both fields are always present in the response: [7](#0-6) 

**Step 4 — The test suite confirms the leak.**

The existing unit test for `RestrictedRead` access explicitly asserts that `rule_id` and `incident_id` are returned for a non-disclosed rule (where `disclosed_at` is `None`): [8](#0-7) 

---

### Impact Explanation

An attacker calling `get_config` as a non-replicated query with an anonymous principal receives a `ConfigResponse` containing, for every rule (including non-disclosed confidential ones):
- `rule_id` — the UUID of the confidential rate-limit rule
- `incident_id` — the UUID of the active security incident it belongs to

With the `incident_id`, the attacker can then call `get_rules_by_incident_id` (also a `#[query]` method, also bypasses `inspect_message`) to enumerate all rule UUIDs linked to that incident. This reveals:
1. That a security incident is actively being rate-limited (i.e., a vulnerability is being exploited or mitigated)
2. The exact incident UUID, enabling correlation across calls and over time
3. The number and IDs of all rules associated with the incident, revealing the scope of the mitigation

This violates the stated confidentiality invariant that non-disclosed rule metadata must not be accessible to unauthorized callers.

---

### Likelihood Explanation

The exploit requires no privileges, no keys, no social engineering, and no protocol-level attack. Any caller with access to the ICP query interface can execute it in a single call. The `get_config` method is publicly listed in the Candid interface as a `query` method, making it trivially discoverable.

---

### Recommendation

The authorization check must be enforced **inside the `get_config` handler itself**, not solely in `inspect_message`. Two complementary fixes:

1. **In `ConfigGetter::get()`**: Return an authorization error (or an empty/stub response) when the access level is `RestrictedRead`, rather than proceeding with partial formatting.
2. **In `ConfigConfidentialityFormatter::format()`**: For non-disclosed rules, also null out `rule_id` and `incident_id` (or omit the rule entirely from the response) when the caller is `RestrictedRead`.

Relying on `inspect_message` alone for access control on query methods is architecturally unsound on ICP, since `inspect_message` is not invoked for non-replicated query calls.

---

### Proof of Concept

```
# Using dfx or any ICP agent, call get_config as a non-replicated query
# with an anonymous principal (no identity required):

dfx canister call <rate_limit_canister_id> get_config '(null)' --query

# Expected: Response contains OutputRule entries where rule_raw = null
# and description = null (redacted), but rule_id and incident_id are
# non-null UUIDs for rules where disclosed_at is None.
#
# Assert: for each rule in response where rule_raw == None,
#         rule_id != "" and incident_id != ""
# This confirms confidential incident UUIDs are leaked to anonymous callers.
```

### Citations

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L31-55)
```rust
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
```

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L111-119)
```rust
fn get_config(version: Option<Version>) -> GetConfigResponse {
    let caller_id = ic_cdk::api::caller();
    let response = with_canister_state(|state| {
        let access_resolver = AccessLevelResolver::new(caller_id, state.clone());
        let formatter = ConfigConfidentialityFormatter;
        let getter = ConfigGetter::new(state, formatter, access_resolver);
        getter.get(&version)
    })?;
    Ok(response)
```

**File:** rs/boundary_node/rate_limits/canister/interface.did (L149-149)
```text
  get_config: (opt Version) -> (GetConfigResponse) query;
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

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L368-374)
```rust
                    rules: vec![
                        api::OutputRule {
                            rule_id: rule_id_1.0.to_string(),
                            incident_id: incident_id.0.to_string(),
                            rule_raw: None,
                            description: None,
                        },
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

**File:** rs/boundary_node/rate_limits/api/src/lib.rs (L109-115)
```rust
#[derive(CandidType, Deserialize, Debug, PartialEq)]
pub struct OutputRule {
    pub rule_id: RuleId,
    pub incident_id: IncidentId,
    pub rule_raw: Option<Vec<u8>>,
    pub description: Option<String>,
}
```
