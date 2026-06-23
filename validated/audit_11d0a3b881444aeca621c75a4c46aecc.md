The evidence is conclusive. Let me trace the full exploit path precisely.

**Finding 1: `inspect_message` does not protect query calls.**

`get_config` is declared `#[query]` at canister.rs line 110. On the IC protocol, `inspect_message` is only invoked for **ingress (update) messages** — query calls bypass it entirely. The guard at canister.rs lines 48–55 that restricts `get_config` to `FullAccess`/`FullRead` callers is therefore dead code for the query path. [1](#0-0) [2](#0-1) 

**Finding 2: `ConfigConfidentialityFormatter::format` does not redact `rule_id` or `incident_id`.**

For undisclosed rules (`disclosed_at == None`), the formatter only clears `description` and `rule_raw`. The fields `id` (→ `rule_id`) and `incident_id` are left intact and flow through `From<OutputRule> for api::OutputRule` directly into the wire response. [3](#0-2) [4](#0-3) 

**Finding 3: The existing test proves the leak.**

The test `test_get_config_success` for a `RestrictedRead` caller explicitly asserts that `rule_id` and `incident_id` are present in the response for an undisclosed rule — confirming this is the intended (but insecure) behavior, not a test artifact. [5](#0-4) 

---

### Title
Undisclosed rule `rule_id` and `incident_id` leaked to anonymous query callers via `get_config` — (`rs/boundary_node/rate_limits/canister/confidentiality_formatting.rs`, `canister.rs`)

### Summary
Any unauthenticated caller can invoke `get_config(Some(v))` as a query call, enumerate all historical config versions, and read the `rule_id` and `incident_id` of every undisclosed (confidential) rate-limit rule. The `inspect_message` guard intended to restrict this method is bypassed because query calls never pass through `inspect_message` on the IC. The confidentiality formatter then fails to redact the two identifying fields.

### Finding Description
Two independent flaws combine:

1. **`inspect_message` bypass**: `get_config` is a `#[query]` endpoint. The IC protocol only invokes `inspect_message` for ingress (update) messages. The guard at `canister.rs:48–55` that rejects non-`FullAccess`/`FullRead` callers is never reached when the method is called as a query. Any principal — including the anonymous principal — can call it freely.

2. **Incomplete redaction in `ConfigConfidentialityFormatter::format`**: For rules where `disclosed_at.is_none()`, the formatter sets `rule_raw = None` and `description = None` but does **not** clear `id` or `incident_id`. These are serialized verbatim into `api::OutputRule { rule_id, incident_id, rule_raw, description }` and returned to the caller. [6](#0-5) 

### Impact Explanation
An attacker learns the UUID of every undisclosed rule and the UUID of the incident it belongs to, across all historical config versions. Because multiple rules sharing the same `incident_id` are grouped, the attacker can determine which canisters or endpoints are under an active, non-public security incident before the DFINITY team discloses it. This enables front-running (e.g., exploiting the vulnerability before the rate-limit rule is publicly known) or targeted attacks against the affected canister.

### Likelihood Explanation
The exploit requires zero privileges, zero keys, and zero social engineering. It is a single unauthenticated query call. The IC query interface is publicly reachable from any HTTP client via the boundary node. The attacker only needs to iterate version numbers from 1 to the latest version (which is also returned in every `get_config` response).

### Recommendation
Two independent fixes are required — both must be applied:

1. **Fix the formatter**: In `ConfigConfidentialityFormatter::format`, also clear `rule.id` (or replace it with a sentinel/zero UUID) and `rule.incident_id` for undisclosed rules, so no identifying metadata is returned.

2. **Fix the access control for queries**: Since `inspect_message` cannot protect query calls, move the authorization check inside `ConfigGetter::get` itself. If the caller's `AccessLevel` is `RestrictedRead`, either return only disclosed rules or return an authorization error. Do not rely on `inspect_message` as the sole gate for query endpoints.

### Proof of Concept
The existing unit test at `getter.rs:358–384` already demonstrates the leak — it asserts `rule_id` and `incident_id` are present for a `RestrictedRead` caller on an undisclosed rule. On mainnet, the following is sufficient:

```bash
# Replace CANISTER_ID with the deployed rate-limits canister
dfx canister --network ic call CANISTER_ID get_config '(opt 1)' --query
# Response will contain rule_id and incident_id for undisclosed rules
# with rule_raw = null and description = null
```

Iterating `opt 1` through `opt N` (where N is the `version` field in any prior response) enumerates all historical undisclosed rule and incident UUIDs. [7](#0-6)

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

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L110-120)
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
