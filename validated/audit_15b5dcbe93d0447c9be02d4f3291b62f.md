Now I have a complete picture. Let me write up the finding.

### Title
`inspect_message` Hook Overly Restricts `get_config` Replicated-Query Access for Unprivileged Callers — (`File: rs/boundary_node/rate_limits/canister/canister.rs`)

### Summary

The `inspect_message` hook in the rate-limits canister blocks any ingress update call to `get_config` from callers who are neither the authorized principal nor an API boundary node. However, the `get_config` method itself is explicitly designed to serve a `RestrictedRead` (filtered, publicly-disclosed-only) response to **any** caller. This is a direct analog to the Phala `balance_of` bug: an access guard is applied at the wrong layer, making a method that should be available to any caller unreachable for unprivileged ingress senders when called as a replicated query.

### Finding Description

The rate-limits canister exposes three read methods — `get_config`, `get_rule_by_id`, and `get_rules_by_incident_id` — all of which are annotated `#[query]` and all of which internally resolve the caller's `AccessLevel` via `AccessLevelResolver`:

- `FullAccess` → authorized principal (write + full read)
- `FullRead` → API boundary node (full read)
- `RestrictedRead` → **any other caller** (publicly disclosed rules only, redacted fields hidden) [1](#0-0) 

The `ConfigGetter` (and `RuleGetter`, `IncidentGetter`) explicitly handles `RestrictedRead` by returning a filtered, redacted response — not an error: [2](#0-1) 

The unit tests confirm that `AccessLevel::RestrictedRead` is a valid, expected path that returns meaningful data: [3](#0-2) 

However, the `inspect_message` hook — which fires for every **ingress update call** before consensus — only accepts `get_config` for `has_full_access || has_full_read_access`. Any other caller is trapped: [4](#0-3) 

`get_rule_by_id` and `get_rules_by_incident_id` are not listed in `UPDATE_METHODS` at all, so they fall into the catch-all trap for every ingress update call: [5](#0-4) 

The `inspect_message` hook does **not** fire for non-replicated query calls (those bypass it entirely). So the inconsistency is:

| Call type | Caller | Result |
|---|---|---|
| Non-replicated query | Any | `RestrictedRead` response (uncertified) |
| Replicated query (update) | Authorized / API BN | Full response (certified) |
| Replicated query (update) | Any other | **Trapped by `inspect_message`** |

The integration test explicitly encodes this broken behavior as an assertion, treating it as correct: [6](#0-5) 

### Impact Explanation

Any unprivileged ingress sender (user or canister calling via ingress) who wants a **certified** (consensus-backed) view of the publicly disclosed rate-limit rules is denied. They are forced to rely on uncertified non-replicated query responses, which a malicious or compromised boundary node can forge or withhold. The `RestrictedRead` path — which the method's own logic fully supports — is unreachable via the replicated path for any caller outside the privileged set. This breaks the trust model for public consumers of the rate-limit configuration.

### Likelihood Explanation

The entry path is trivially reachable: any unprivileged principal submitting an ingress update call to `get_config` on the rate-limits canister triggers the trap. No special privileges, keys, or network-level access are required. The only prerequisite is knowing the canister ID and method name.

### Recommendation

Remove the `get_config` restriction from the `inspect_message` guard so that any caller may invoke it as a replicated query and receive the `RestrictedRead` filtered response. The method-level `AccessLevelResolver` already enforces the correct confidentiality boundary. Similarly, add `get_rule_by_id` and `get_rules_by_incident_id` to the accepted methods list (or remove them from the catch-all trap) so unprivileged callers can obtain certified responses for those endpoints as well.

Concretely, the `inspect_message` block for `get_config` should be changed from:

```rust
if called_method == REPLICATED_QUERY_METHOD {
    if has_full_access || has_full_read_access {
        ic_cdk::api::call::accept_message();
    } else {
        ic_cdk::api::trap("...");  // ← incorrect: blocks RestrictedRead callers
    }
}
```

to simply:

```rust
if called_method == REPLICATED_QUERY_METHOD
    || called_method == "get_rule_by_id"
    || called_method == "get_rules_by_incident_id"
{
    ic_cdk::api::call::accept_message(); // method-level logic handles access
}
```

### Proof of Concept

1. Deploy the rate-limits canister with any `authorized_principal`.
2. As an unprivileged principal (neither the authorized principal nor an API boundary node), submit an ingress **update** call to `get_config` with `None` as the version argument.
3. Observe the call is rejected pre-consensus with `"message_inspection_failed: method call is prohibited in the current context"`.
4. Submit the same call as a **query** (non-replicated) — it succeeds and returns a `RestrictedRead` filtered response.

The discrepancy proves that the `inspect_message` guard is the sole barrier: the method logic itself accepts and correctly handles unprivileged callers, but the pre-consensus hook prevents them from ever reaching it via the replicated path. [7](#0-6) [8](#0-7)

### Citations

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

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L34-68)
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

**File:** rs/boundary_node/rate_limits/integration_tests/tests/rate_limit_canister_tests.rs (L79-96)
```rust
    // 1.b. As an unauthorized principal, try executing `get_config()` query as an update call; assert this call is also rejected
    let input_version = Encode!(&None::<Version>).unwrap();
    let response: Result<(), String> = canister_call(
        &pocket_ic,
        "get_config",
        "update",
        canister_id,
        Principal::from_text(PRINCIPAL_2).unwrap(),
        input_version,
    )
    .await;

    let err_msg = response.unwrap_err();
    assert!(
        err_msg.contains(
            "message_inspection_failed: method call is prohibited in the current context"
        )
    );
```
