Looking at the code carefully across `canister.rs`, `confidentiality_formatting.rs`, `getter.rs`, and `types.rs`.

### Title
Undisclosed Rule Metadata (`rule_id`, `incident_id`, `added_in_version`, `removed_in_version`) Leaked to Any Anonymous Caller via Unenforced `inspect_message` Guard on Query Calls — (`rs/boundary_node/rate_limits/canister/canister.rs`, `confidentiality_formatting.rs`, `getter.rs`)

---

### Summary

The rate-limit canister's `inspect_message` hook is intended to restrict `get_config` to `FullAccess` or `FullRead` callers. However, `inspect_message` is only invoked by the IC runtime for **update/ingress calls**, never for **query calls**. Since `get_config`, `get_rule_by_id`, and `get_rules_by_incident_id` are all declared `#[query]`, any anonymous principal can call them directly, bypassing the guard entirely. The `ConfigConfidentialityFormatter` and `RuleConfidentialityFormatter` then only redact `rule_raw` and `description`, leaving `rule_id` and `incident_id` fully populated in every undisclosed rule returned by `get_config`, and additionally `added_in_version` and `removed_in_version` in every undisclosed rule returned by `get_rule_by_id` / `get_rules_by_incident_id`.

---

### Finding Description

**Step 1 — `inspect_message` is a dead guard for query calls.**

`get_config` is registered as `#[query]`: [1](#0-0) 

The `inspect_message` hook fires only for ingress (update) messages. The IC protocol never invokes it for non-replicated query calls. The constant name `REPLICATED_QUERY_METHOD` hints at the intent to restrict replicated-query usage, but the `#[query]` annotation means the method is reachable as a plain query without any ingress path: [2](#0-1) 

An anonymous caller sending a regular query to `get_config` never triggers lines 48–55.

**Step 2 — `ConfigConfidentialityFormatter` only redacts two fields.**

For `RestrictedRead` callers, `ConfigGetter::get()` calls `self.formatter.format(config)`: [3](#0-2) 

`ConfigConfidentialityFormatter::format()` sets `is_redacted = true` and nulls `description` and `rule_raw` for undisclosed rules, but leaves `rule_id` and `incident_id` intact: [4](#0-3) 

**Step 3 — `api::OutputRule` carries `rule_id` and `incident_id` in the wire response.**

The public API type confirms both fields are always serialized: [5](#0-4) 

The internal-to-API conversion does not suppress them: [6](#0-5) 

**Step 4 — `added_in_version` / `removed_in_version` are additionally leaked via `get_rule_by_id` and `get_rules_by_incident_id`.**

Both are also `#[query]` methods, equally unguarded. `RuleConfidentialityFormatter` also only nulls `rule_raw` and `description`: [7](#0-6) 

`api::OutputRuleMetadata` carries `added_in_version` and `removed_in_version` unconditionally: [8](#0-7) 

The existing unit test for `RestrictedRead` explicitly confirms these fields are returned for undisclosed rules: [9](#0-8) 

---

### Impact Explanation

Any anonymous principal can:
1. Call `get_config` as a query → enumerate `rule_id` and `incident_id` for every undisclosed rate-limit rule.
2. Call `get_rule_by_id` with each leaked `rule_id` → obtain `added_in_version` and `removed_in_version`, revealing exactly when each confidential rule was introduced and retired.
3. Call `get_rules_by_incident_id` with each leaked `incident_id` → enumerate all rules grouped by incident, including their version timeline.

This allows correlation of rate-limit enforcement events with network incidents before public disclosure, defeating the entire confidentiality model of the canister.

---

### Likelihood Explanation

The attack requires no privileges, no keys, and no social engineering. It is a single unauthenticated query call to a public canister endpoint. Any IC client library can reproduce it. The `inspect_message` guard gives a false sense of security but is structurally inert for query calls.

---

### Recommendation

1. **Move access control into the query handler itself**, not into `inspect_message`. In `get_config`, `get_rule_by_id`, and `get_rules_by_incident_id`, check `access_resolver.get_access_level()` and return an authorization error (or an empty/stub response) for `RestrictedRead` callers before any data is fetched.
2. **Extend `ConfigConfidentialityFormatter::format()`** to also null out `rule_id` and `incident_id` for undisclosed rules if the design intent is that their existence must not be observable.
3. **Extend `RuleConfidentialityFormatter::format()`** to also null out `added_in_version` and `removed_in_version` for undisclosed rules.
4. Remove or document the `inspect_message` guard for `REPLICATED_QUERY_METHOD` to avoid misleading future maintainers.

---

### Proof of Concept

```
# 1. Populate canister with one undisclosed rule (authorized caller, update call)
dfx canister call rate_limits add_config '(record { schema_version=1; rules=vec { record { incident_id="<uuid>"; rule_raw=blob "{}"; description="secret" } } })'

# 2. Call get_config as anonymous principal via regular query — inspect_message is NOT invoked
dfx canister call --query rate_limits get_config '(null)' --no-wallet

# Expected: response contains is_redacted=true, rule_raw=null, description=null
# BUT rule_id and incident_id are fully populated for the undisclosed rule

# 3. Use the leaked rule_id to call get_rule_by_id
dfx canister call --query rate_limits get_rule_by_id '("<leaked-rule-id>")' --no-wallet

# Expected: added_in_version and removed_in_version are returned for the undisclosed rule
```

The existing test at `getter.rs` lines 358–384 already documents the exact redacted shape returned to `RestrictedRead` callers, confirming `rule_id` and `incident_id` are present. The only missing piece in the test is that it does not assert these fields are absent — because the code never removes them. [10](#0-9)

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

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L110-111)
```rust
#[query]
fn get_config(version: Option<Version>) -> GetConfigResponse {
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

**File:** rs/boundary_node/rate_limits/api/src/lib.rs (L117-126)
```rust
#[derive(CandidType, Deserialize, Debug, PartialEq)]
pub struct OutputRuleMetadata {
    pub rule_id: RuleId,
    pub incident_id: IncidentId,
    pub rule_raw: Option<Vec<u8>>,
    pub description: Option<String>,
    pub disclosed_at: Option<Timestamp>,
    pub added_in_version: Version,
    pub removed_in_version: Option<Version>,
}
```

**File:** rs/boundary_node/rate_limits/canister/types.rs (L78-87)
```rust
impl From<OutputRule> for api::OutputRule {
    fn from(value: OutputRule) -> Self {
        api::OutputRule {
            description: value.description,
            rule_id: value.id.to_string(),
            incident_id: value.incident_id.to_string(),
            rule_raw: value.rule_raw,
        }
    }
}
```
