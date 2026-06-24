### Title
`inspect_message`-Only Authorization Bypassed via Inter-Canister Calls — (`rs/boundary_node/rate_limits/canister/canister.rs`)

---

### Summary

The rate-limits canister uses `canister_inspect_message` as the sole access-control gate for its read-only query methods (`get_config`, `get_rule_by_id`, `get_rules_by_incident_id`). Because `inspect_message` is only invoked for **ingress messages** and is never called for **inter-canister calls**, any on-chain canister can bypass the restriction and retrieve confidential rate-limit rule metadata by issuing a composite-query inter-canister call directly to those methods.

---

### Finding Description

The `inspect_message` hook in `rs/boundary_node/rate_limits/canister/canister.rs` enforces that `get_config` (the `REPLICATED_QUERY_METHOD`) is only callable by API boundary-node principals or the single authorized principal: [1](#0-0) 

The three read-only query methods themselves contain **no rejection logic** for unauthorized callers. They unconditionally construct an `AccessLevelResolver`, resolve the caller's level, and return data — applying only confidentiality *formatting* (redaction) for `RestrictedRead` callers, not an outright error: [2](#0-1) 

The `ConfigGetter` confirms this: for `RestrictedRead` callers it returns a response with `is_redacted: true` and `rule_raw`/`description` set to `None` for non-disclosed rules, but it still returns the full list of rules including their `rule_id`, `incident_id`, `added_in_version`, and `removed_in_version`: [3](#0-2) 

The IC execution environment explicitly documents and tests that `inspect_message` is **not** invoked for inter-canister calls: [4](#0-3) 

The `execute_inspect_message` function takes a `SignedIngressContent` argument — it is structurally impossible to invoke it for a canister-to-canister message: [5](#0-4) 

By contrast, the two **update** methods (`add_config`, `disclose_rules`) are safe: they carry their own `WithAuthorization` wrapper that independently checks the caller's access level at execution time: [6](#0-5) [7](#0-6) 

---

### Impact Explanation

A malicious canister issues a **composite-query** inter-canister call to `get_config`, `get_rule_by_id`, or `get_rules_by_incident_id` on the rate-limits canister. `inspect_message` is never invoked. The query executes with `RestrictedRead` access. The response leaks:

- The **existence** of every non-disclosed (confidential) rate-limit rule.
- The `rule_id` and `incident_id` of each confidential rule.
- The `added_in_version` / `removed_in_version` version numbers of confidential rules.

The actual `rule_raw` and `description` fields are redacted, but the structural metadata of confidential security rules — which are supposed to be invisible to unauthorized parties — is exposed. This undermines the confidentiality model of the rate-limit system, which is designed to keep undisclosed rules secret until explicitly disclosed. [8](#0-7) 

---

### Likelihood Explanation

Any canister deployed on the IC can issue a composite-query call to the rate-limits canister. No privileged role, key material, or subnet-majority corruption is required. The attacker only needs to know the canister ID of the rate-limits canister (which is public). The attack is deterministic and repeatable.

---

### Recommendation

Add explicit caller-identity checks inside the query function bodies, mirroring the `WithAuthorization` pattern already used for `add_config` and `disclose_rules`. For `RestrictedRead` callers, return an `Unauthorized` error rather than a redacted response. For example:

```rust
#[query]
fn get_config(version: Option<Version>) -> GetConfigResponse {
    let caller_id = ic_cdk::api::caller();
    with_canister_state(|state| {
        let access_resolver = AccessLevelResolver::new(caller_id, state.clone());
        // Reject callers that have neither FullAccess nor FullRead
        if access_resolver.get_access_level() == AccessLevel::RestrictedRead {
            return Err(GetConfigError::Unauthorized);
        }
        let formatter = ConfigConfidentialityFormatter;
        let getter = ConfigGetter::new(state, formatter, access_resolver);
        getter.get(&version)
    })?;
    Ok(response)
}
```

Apply the same pattern to `get_rule_by_id` and `get_rules_by_incident_id`. The `inspect_message` hook should be treated as a **best-effort pre-consensus filter** (to save cycles), not as the authoritative access-control boundary.

---

### Proof of Concept

1. Deploy an attacker canister on any subnet.
2. From the attacker canister, issue a composite-query call to the rate-limits canister's `get_config` method.
3. `inspect_message` is not invoked (inter-canister path).
4. `get_config` executes; `AccessLevelResolver` resolves the attacker canister's principal to `RestrictedRead`.
5. `ConfigGetter::get` returns a `ConfigResponse` with `is_redacted: true` but with all rule entries present, each carrying `rule_id`, `incident_id`, `added_in_version`, and `removed_in_version` — including rules that have never been publicly disclosed.

The test at `rs/execution_environment/src/execution_environment/tests.rs:2754` explicitly confirms that a canister whose `inspect_message` rejects all ingress messages is still fully reachable via inter-canister calls, establishing the bypass as a guaranteed IC protocol property, not a speculative edge case. [9](#0-8)

### Citations

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

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L152-182)
```rust
#[update]
fn add_config(config: InputConfig) -> AddConfigResponse {
    let caller_id = ic_cdk::api::caller();
    let current_time = ic_cdk::api::time();
    with_canister_state(|state| {
        let access_resolver = AccessLevelResolver::new(caller_id, state.clone());
        let adder = ConfigAdder::new(state);
        let adder = WithAuthorization::new(adder, access_resolver);
        let adder = WithMetrics::new(adder);
        adder.add_config(config, current_time)
    })?;
    Ok(())
}

/// Makes specified rules publicly accessible for viewing
///
/// This update method allows authorized callers to disclose rules or incidents (collection of rules),
/// making them viewable by the public. It includes authorization check and metrics collection.
#[update]
fn disclose_rules(args: DiscloseRulesArg) -> DiscloseRulesResponse {
    let caller_id = ic_cdk::api::caller();
    let disclose_time = ic_cdk::api::time();
    with_canister_state(|state| {
        let access_resolver = AccessLevelResolver::new(caller_id, state.clone());
        let discloser = RulesDiscloser::new(state);
        let discloser = WithAuthorization::new(discloser, access_resolver);
        let discloser = WithMetrics::new(discloser);
        discloser.disclose_rules(args, disclose_time)
    })?;
    Ok(())
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

**File:** rs/execution_environment/src/execution_environment/tests.rs (L2739-2774)
```rust
#[test]
fn can_reject_all_ingress_messages() {
    let mut test = ExecutionTestBuilder::new().build();
    let canister = test.universal_canister().unwrap();
    test.ingress(
        canister,
        "update",
        wasm().set_inspect_message(wasm().build()).reply().build(),
    )
    .unwrap();
    let err = test
        .should_accept_ingress_message(canister, "", vec![])
        .unwrap_err();
    assert_eq!(ErrorCode::CanisterRejectedMessage, err.code());

    // Inter-canister calls still work.
    let caller = test.universal_canister().unwrap();
    let res = test
        .ingress(
            caller,
            "update",
            wasm().inter_update(canister, CallArgs::default()).build(),
        )
        .unwrap();
    let expected_reply = [
        b"Hello ",
        caller.get().as_slice(),
        b" this is ",
        canister.get().as_slice(),
    ]
    .concat();
    match res {
        WasmResult::Reply(data) => assert_eq!(data, expected_reply),
        WasmResult::Reject(msg) => panic!("Unexpected reject: {msg}"),
    };
}
```

**File:** rs/execution_environment/src/execution/inspect_message.rs (L22-34)
```rust
pub fn execute_inspect_message(
    time: Time,
    canister: CanisterState,
    ingress: &SignedIngressContent,
    execution_parameters: ExecutionParameters,
    subnet_available_memory: SubnetAvailableMemory,
    hypervisor: &Hypervisor,
    network_topology: &NetworkTopology,
    logger: &ReplicaLogger,
    state_changes_error: &IntCounter,
    ingress_filter_metrics: &IngressFilterMetrics,
    subnet_cycles_config: CyclesAccountManagerSubnetConfig,
) -> (NumInstructions, Result<(), UserError>) {
```

**File:** rs/boundary_node/rate_limits/canister/access_control.rs (L72-99)
```rust
impl<T: AddsConfig, R: ResolveAccessLevel> AddsConfig for WithAuthorization<T, R> {
    fn add_config(
        &self,
        input_config: rate_limits_api::InputConfig,
        time: Timestamp,
    ) -> Result<(), AddConfigError> {
        // Only privileged users can perform this operation
        if self.access_resolver.get_access_level() == AccessLevel::FullAccess {
            // Perform the inner call only if authorized.
            return self.inner.add_config(input_config, time);
        }
        Err(AddConfigError::Unauthorized)
    }
}

impl<T: DisclosesRules, R: ResolveAccessLevel> DisclosesRules for WithAuthorization<T, R> {
    fn disclose_rules(
        &self,
        arg: rate_limits_api::DiscloseRulesArg,
        current_time: Timestamp,
    ) -> Result<(), DiscloseRulesError> {
        // Only privileged users can perform this operation
        if self.access_resolver.get_access_level() == AccessLevel::FullAccess {
            return self.inner.disclose_rules(arg, current_time);
        }
        Err(DiscloseRulesError::Unauthorized)
    }
}
```

**File:** rs/boundary_node/rate_limits/canister/confidentiality_formatting.rs (L14-28)
```rust
impl ConfidentialityFormatting for ConfigConfidentialityFormatter {
    type Input = OutputConfig;

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
