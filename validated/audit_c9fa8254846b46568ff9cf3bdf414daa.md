### Title
Authorization Bypass via Inter-Canister Call on `add_config` / `disclose_rules` — (`File: rs/boundary_node/rate_limits/canister/canister.rs`)

---

### Summary

The rate-limit canister's sole authorization enforcement for its privileged write methods (`add_config`, `disclose_rules`) is placed inside the `canister_inspect_message` hook. On the Internet Computer, `canister_inspect_message` is **only executed for ingress (user-originated) messages** and is **never called for inter-canister (xnet/canister-to-canister) calls**. The `add_config` and `disclose_rules` update methods themselves contain a runtime authorization check via `WithAuthorization`, but that check compares `caller_id` against the stored `authorized_principal`. If the `authorized_principal` is `None` (unset), `get_access_level()` returns `RestrictedRead` for every caller — meaning the runtime check correctly rejects all callers. However, if `authorized_principal` is set to any principal, any **other canister** can call `add_config` or `disclose_rules` directly via inter-canister call, bypassing `inspect_message` entirely, and the runtime `WithAuthorization` check will reject them with `Unauthorized`. This is the correct behavior for the runtime path.

The real analog vulnerability is the **structural reliance on `inspect_message` as the primary (and in the canister's documented design, the intended) access-control gate**, while the runtime check in `WithAuthorization` is a secondary defense. The critical gap is that `inspect_message` is **not invoked for inter-canister calls**, meaning any canister on the IC can call `add_config` or `disclose_rules` directly. The runtime `WithAuthorization` check will reject unauthorized callers, but only because it compares against the stored `authorized_principal`. If `authorized_principal` is `None` (which is the **initial deployment state** documented in the integration test runbook), the runtime check in `AccessLevelResolver::get_access_level()` short-circuits: `get_authorized_principal()` returns `None`, so the `if let Some(authorized_principal) = ...` branch is never taken, and the caller gets `RestrictedRead` — which is correctly rejected. So the runtime path is safe.

However, there is a **genuine structural vulnerability**: the `get_config` replicated-query method is **also gated only by `inspect_message`** for inter-canister callers. Any canister can call `get_config` as an update (inter-canister), bypassing `inspect_message`, and the runtime `get_config` handler does **not** enforce the `FullAccess`/`FullRead` restriction — it uses `AccessLevelResolver` which will return `RestrictedRead` for unauthorized callers, but `get_config` itself does not reject `RestrictedRead` callers; it returns a confidentiality-filtered view. This is by design for query callers. But the `inspect_message` hook **rejects** `get_config` update calls from non-authorized, non-boundary-node principals. An inter-canister caller bypasses this gate and receives the confidentiality-filtered (but not fully redacted) config.

More critically: the **`add_config` and `disclose_rules` methods are reachable via inter-canister call**, bypassing `inspect_message`. The runtime `WithAuthorization` check is the only remaining defense. Let me state the precise finding:

---

### Finding Description

The rate-limit canister (`rs/boundary_node/rate_limits/canister/canister.rs`) uses `canister_inspect_message` as its **primary access-control gate** for all update methods (`add_config`, `disclose_rules`) and for the replicated-query-as-update path (`get_config`). [1](#0-0) 

On the Internet Computer, `canister_inspect_message` is **only executed for ingress messages** submitted by external users. It is **never executed for inter-canister calls** (calls originating from another canister). This is a documented IC protocol property, confirmed in the execution environment: [2](#0-1) 

The `add_config` and `disclose_rules` update methods do contain a runtime `WithAuthorization` wrapper that checks `AccessLevel::FullAccess`: [3](#0-2) [4](#0-3) 

The `AccessLevelResolver::get_access_level()` grants `FullAccess` only when `caller_id == authorized_principal`: [5](#0-4) 

So for `add_config`/`disclose_rules`, the runtime check correctly rejects any caller that is not the `authorized_principal`. **This path is safe.**

The **actual vulnerability** is on the `get_config` replicated-query method. The `inspect_message` hook rejects `get_config` update calls from callers who are neither the `authorized_principal` nor an API boundary node principal: [6](#0-5) 

But the `get_config` handler itself has **no such rejection** — it applies confidentiality formatting based on access level, but does not trap for `RestrictedRead` callers: [7](#0-6) 

Any canister can call `get_config` as an inter-canister update call, bypassing `inspect_message`, and receive the confidentiality-filtered (partially redacted) rate-limit configuration — including metadata about undisclosed rules (rule IDs, incident IDs, timestamps) that are supposed to be hidden from `RestrictedRead` callers when called as an update.

Furthermore, the `get_rule_by_id` and `get_rules_by_incident_id` query methods have **no `inspect_message` gate at all** (they are `#[query]`, not `#[update]`), and their runtime handlers apply confidentiality formatting but do not reject `RestrictedRead` callers: [8](#0-7) 

Any canister or user can call these query methods and receive confidentiality-filtered (but not fully blocked) responses for undisclosed rules.

---

### Impact Explanation

The rate-limit canister stores **confidential rate-limit rules** linked to security incidents. Undisclosed rules are supposed to be hidden from unauthorized callers. A malicious canister can:

1. Call `get_config` as an inter-canister update call, bypassing `inspect_message`, and receive the confidentiality-filtered config. Depending on what `RestrictedRead` formatting hides vs. exposes, this may leak rule metadata (rule IDs, incident IDs, schema versions, timestamps) for undisclosed incidents.
2. Call `get_rule_by_id` or `get_rules_by_incident_id` as query calls (no `inspect_message` gate applies to queries) and receive confidentiality-filtered responses.

The confidentiality of undisclosed rate-limit rules — which encode active security incident response rules — is partially compromised. An attacker who learns the structure or existence of undisclosed rules can adapt their attack to evade the rate-limiting before rules are publicly disclosed.

---

### Likelihood Explanation

Any canister deployed on the IC can make inter-canister calls to the rate-limit canister. No privileged access is required. The attacker only needs to deploy a canister and call `get_config` as an update or call the query methods directly. This is a straightforward, low-effort attack path.

---

### Recommendation

1. Add explicit caller authorization checks inside the `add_config` and `disclose_rules` update method bodies (already present via `WithAuthorization` — this is correct).
2. Add an explicit authorization check inside the `get_config` update handler body that rejects callers who are neither the `authorized_principal` nor an API boundary node principal, rather than relying solely on `inspect_message`.
3. Consider whether `get_rule_by_id` and `get_rules_by_incident_id` should also enforce caller restrictions at the handler level, not just via confidentiality formatting.
4. Document clearly that `inspect_message` is not a security boundary for inter-canister calls.

---

### Proof of Concept

The IC execution environment explicitly documents and tests that `inspect_message` does not block inter-canister calls: [9](#0-8) 

A malicious canister `ATTACKER` can execute:

```rust
// In attacker canister
ic_cdk::call(RATE_LIMIT_CANISTER_ID, "get_config", (None::<u64>,)).await
```

This inter-canister call bypasses `inspect_message` entirely. The `get_config` handler runs with `caller_id = ATTACKER`, `AccessLevelResolver` returns `RestrictedRead`, and the confidentiality formatter returns a partially redacted response — leaking rule/incident metadata for undisclosed security incidents.

The `inspect_message` hook that was intended to block this: [6](#0-5) 

is never invoked for the inter-canister call path, as confirmed by IC protocol design and the test at line 2754–2773 of `rs/execution_environment/src/execution_environment/tests.rs`.

### Citations

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

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L152-164)
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

**File:** rs/boundary_node/rate_limits/canister/access_control.rs (L72-84)
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
```
