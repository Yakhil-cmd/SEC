Looking at the actual code, I need to trace the exact call paths and what data is exposed.

### Title
`inspect_message` Guard for `get_config` Bypassed via Regular Query Call, Exposing All Rule and Incident UUIDs — (`rs/boundary_node/rate_limits/canister/canister.rs`)

---

### Summary

The `inspect_message` hook is intended to block `RestrictedRead` callers from invoking `get_config`. However, because `get_config` is declared as a `#[query]` method, it is reachable via regular (non-replicated) IC query calls, which never trigger `inspect_message`. Any unprivileged caller can therefore call `get_config` as a query, bypass the guard entirely, and receive a response that includes all `rule_id` and `incident_id` UUIDs — including those for non-disclosed (confidential) rules.

---

### Finding Description

**Step 1 — The guard only fires for ingress update messages.**

The `#[inspect_message]` hook is an IC protocol mechanism that runs only for ingress messages (update calls). It does not run for query calls. This is a fundamental property of the IC execution model.

In `canister.rs`, the guard is defined as:

```rust
const REPLICATED_QUERY_METHOD: &str = "get_config";

#[inspect_message]
fn inspect_message() {
    ...
    if called_method == REPLICATED_QUERY_METHOD {
        if has_full_access || has_full_read_access {
            ic_cdk::api::call::accept_message();
        } else {
            ic_cdk::api::trap("message_inspection_failed: ...");
        }
    }
    ...
}
``` [1](#0-0) 

**Step 2 — `get_config` is declared `#[query]`, not `#[update]`.**

```rust
#[query]
fn get_config(version: Option<Version>) -> GetConfigResponse {
``` [2](#0-1) 

A `#[query]` method is callable as a regular (non-replicated) query. Regular queries bypass `inspect_message` entirely. The variable name `REPLICATED_QUERY_METHOD` signals the design intent that `get_config` should be called as a replicated query (i.e., via an update call), but the `#[query]` annotation makes it reachable without that path.

**Step 3 — The in-function access control does NOT redact `rule_id` or `incident_id`.**

When a `RestrictedRead` caller reaches `ConfigGetter::get()`, the code applies `ConfigConfidentialityFormatter::format()`:

```rust
// Hide non-disclosed rules from unauthorized viewers.
Ok(api::ConfigResponse {
    version,
    active_since: stored_config.active_since,
    config: self.formatter.format(config).into(),
})
``` [3](#0-2) 

The formatter only redacts `description` and `rule_raw` for non-disclosed rules. It does **not** redact `rule_id` or `incident_id`:

```rust
fn format(&self, config: OutputConfig) -> OutputConfig {
    let mut config = config;
    config.is_redacted = true;
    config.rules.iter_mut().for_each(|rule| {
        if rule.disclosed_at.is_none() {
            rule.description = None;
            rule.rule_raw = None;
        }
    });
    config
}
``` [4](#0-3) 

The `OutputRule` struct (and its API conversion) always includes `rule_id` and `incident_id` unconditionally:

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
``` [5](#0-4) 

---

### Impact Explanation

Any unprivileged caller (anonymous or otherwise) can:
1. Send `get_config` as a regular IC query call.
2. Receive a structurally complete `ConfigResponse` with `is_redacted: true`.
3. Enumerate all `rule_id` and `incident_id` UUIDs across all versions, including those for rules that have never been publicly disclosed.

The `inspect_message` guard for `get_config` is rendered dead code for the regular query path. The intended invariant — that only registered API boundary nodes (`FullRead`) or the authorized principal (`FullAccess`) may call `get_config` — is violated.

The actual rate-limit rule payloads (`rule_raw`, `description`) for non-disclosed rules remain hidden by the formatter. The impact is scoped to UUID enumeration of confidential rule and incident identifiers.

---

### Likelihood Explanation

This is trivially exploitable. Any IC client can issue a query call to `get_config` without any credentials. No privileged access, key material, or social engineering is required. The bypass is a direct consequence of the `#[query]` annotation on a method the `inspect_message` guard treats as update-only.

---

### Recommendation

Either:
- Change `get_config` from `#[query]` to `#[update]` so that `inspect_message` fires for all calls to it (consistent with the `REPLICATED_QUERY_METHOD` naming intent), **or**
- Add an explicit caller authorization check inside `get_config` itself that traps `RestrictedRead` callers, making the in-function guard independent of call type.

Additionally, the `ConfigConfidentialityFormatter` should be updated to also redact `rule_id` and `incident_id` for non-disclosed rules when serving `RestrictedRead` callers, as a defense-in-depth measure.

---

### Proof of Concept

```
// Differential test (pseudocode using ic-agent or dfx)

// 1. Call get_config as a regular query (bypasses inspect_message)
let response_query = agent.query(&canister_id, "get_config").call().await;
assert!(response_query.is_ok());
// response contains all rule_ids and incident_ids, is_redacted=true

// 2. Call get_config as an update (goes through inspect_message)
let response_update = agent.update(&canister_id, "get_config").call_and_wait().await;
assert!(response_update.is_err()); // trapped: "message_inspection_failed"

// The two paths produce different authorization outcomes for the same RestrictedRead caller.
// Via query: succeeds, exposes all UUIDs.
// Via update: trapped by inspect_message.
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

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L142-148)
```rust
        // Hide non-disclosed rules from unauthorized viewers.
        Ok(api::ConfigResponse {
            version,
            active_since: stored_config.active_since,
            config: self.formatter.format(config).into(),
        })
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
