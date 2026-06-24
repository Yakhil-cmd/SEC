Looking at the actual production code to trace the exact exploit path.

### Title
Stale `API_BOUNDARY_NODE_PRINCIPALS` Cache Allows Removed BN to Read Non-Disclosed Rate-Limit Rules During Polling Window — (`rs/boundary_node/rate_limits/canister/canister.rs`)

---

### Summary

The rate-limits canister maintains a heap-resident `HashSet<Principal>` (`API_BOUNDARY_NODE_PRINCIPALS`) that is populated by periodic inter-canister calls to the registry. Both the `inspect_message` hook (for update/replicated-query calls to `get_config`) and the in-execution `AccessLevelResolver` (for plain query calls) consult this same stale set. Between a BN's removal from the registry and the canister's next successful poll, the removed BN's principal remains in the set, granting it `AccessLevel::FullRead` and full access to non-disclosed (confidential) rate-limit rules.

---

### Finding Description

**Entrypoint — `inspect_message`:**

The `inspect_message` hook gates the `get_config` replicated-query method:

```rust
// canister.rs:34-55
#[inspect_message]
fn inspect_message() {
    let caller_id: Principal = ic_cdk::api::caller();
    let called_method = ic_cdk::api::call::method_name();

    let (has_full_access, has_full_read_access) = with_canister_state(|state| {
        ...
        state.is_api_boundary_node_principal(&caller_id),   // ← stale check
    });

    if called_method == REPLICATED_QUERY_METHOD {
        if has_full_access || has_full_read_access {
            ic_cdk::api::call::accept_message();             // ← passes removed BN
        } else {
            ic_cdk::api::trap(...);
        }
    }
}
``` [1](#0-0) 

**The stale set:**

`API_BOUNDARY_NODE_PRINCIPALS` is a heap-resident `thread_local! { RefCell<HashSet<Principal>> }`, not in stable memory. It is only updated by the periodic timer:

```rust
// storage.rs:149
pub static API_BOUNDARY_NODE_PRINCIPALS: RefCell<HashSet<Principal>> = RefCell::new(HashSet::new());
``` [2](#0-1) 

```rust
// canister.rs:251-274
fn periodically_poll_api_boundary_nodes(interval: u64, canister_api: Arc<dyn CanisterApi>) {
    let interval = Duration::from_secs(interval);
    ic_cdk_timers::set_timer_interval(interval, move || {
        ...
        canister_api.set_api_boundary_nodes_principals(
            api_bn_records.into_iter().filter_map(|n| n.id).collect(),
        );
        ...
    });
}
``` [3](#0-2) 

**The `is_api_boundary_node_principal` check reads from this stale set:**

```rust
// state.rs:125-128
fn is_api_boundary_node_principal(&self, principal: &Principal) -> bool {
    self.api_boundary_node_principals
        .with(|cell| cell.borrow().contains(principal))
}
``` [4](#0-3) 

**What `FullRead` exposes:**

In `getter.rs`, a caller with `FullRead` access receives the complete unredacted config including non-disclosed rules (those with `disclosed_at: None`):

```rust
// getter.rs:130-139
let is_authorized_viewer = self.access_resolver.get_access_level()
    == AccessLevel::FullAccess
    || self.access_resolver.get_access_level() == AccessLevel::FullRead;

if is_authorized_viewer {
    return Ok(api::ConfigResponse { version, active_since, config: config.into() });
}
``` [5](#0-4) 

Unauthorized callers only see rules where `disclosed_at.is_some()`:

```rust
// confidentiality_formatting.rs:21-26
config.rules.iter_mut().for_each(|rule| {
    if rule.disclosed_at.is_none() {
        rule.description = None;
        rule.rule_raw = None;
    }
});
``` [6](#0-5) 

**The same stale check also applies to plain query calls** — `get_config` is a `#[query]` method and `inspect_message` is not invoked for queries; the `AccessLevelResolver` inside `get_config` itself performs the same `is_api_boundary_node_principal` lookup against the same stale set: [7](#0-6) 

---

### Impact Explanation

A removed BN retains `FullRead` access to all non-disclosed rate-limit rules (containing `rule_raw` payloads and `description` fields describing active security incidents) for the entire duration of the polling interval. These rules are explicitly kept non-disclosed to prevent attackers from learning about active mitigations (e.g., blocked IP ranges, attack signatures). A removed/compromised BN operator can read this data and use it to evade rate-limiting enforcement.

---

### Likelihood Explanation

The polling interval is operator-configurable (`registry_polling_period_secs`). The integration test sets it to 1 second, but production deployments may use much longer intervals. The exploit requires only that the attacker possess the private key of a BN that has been removed from the registry — a realistic scenario when a BN is decommissioned or removed after compromise. No governance majority, threshold attack, or external dependency is required.

---

### Recommendation

1. **Shorten the polling interval** to minimize the stale window.
2. **Add a canister-level revocation mechanism**: allow the authorized principal to explicitly remove a BN principal from `API_BOUNDARY_NODE_PRINCIPALS` without waiting for the next poll.
3. **Re-check access at execution time** inside `get_config` by making a synchronous registry call (or caching with a TTL shorter than the polling interval) rather than relying solely on the periodic poll.
4. **Consider moving `API_BOUNDARY_NODE_PRINCIPALS` to stable memory** with a versioned update so that upgrades do not reset the set to empty (currently the set is heap-resident and lost on upgrade, requiring a fresh poll).

---

### Proof of Concept

```
1. Install rate-limits canister with registry_polling_period_secs = 300.
2. Add BN_A to registry; wait for canister to poll → BN_A is in API_BOUNDARY_NODE_PRINCIPALS.
3. Remove BN_A from registry (governance proposal executed).
4. Immediately (before next poll at t+300s), call get_config as BN_A (update call).
5. inspect_message checks stale API_BOUNDARY_NODE_PRINCIPALS → BN_A still present → accept_message().
6. get_config executes with AccessLevel::FullRead → returns full unredacted config including non-disclosed rules.
7. Assert: response.config.is_redacted == false and non-disclosed rule_raw fields are populated.
```

### Citations

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L34-55)
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
```

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L111-120)
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
}
```

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L251-274)
```rust
fn periodically_poll_api_boundary_nodes(interval: u64, canister_api: Arc<dyn CanisterApi>) {
    let interval = Duration::from_secs(interval);

    ic_cdk_timers::set_timer_interval(interval, move || {
        let canister_api = canister_api.clone();

        async move {
            let canister_id = Principal::from(REGISTRY_CANISTER_ID);

            let (call_status, message) = match call::<
                _,
                (Result<Vec<ApiBoundaryNodeIdRecord>, String>,),
            >(
                canister_id,
                REGISTRY_CANISTER_METHOD,
                (&GetApiBoundaryNodeIdsRequest {},),
            )
            .await
            {
                Ok((Ok(api_bn_records),)) => {
                    // Set authorized readers of the rate-limit config.
                    canister_api.set_api_boundary_nodes_principals(
                        api_bn_records.into_iter().filter_map(|n| n.id).collect(),
                    );
```

**File:** rs/boundary_node/rate_limits/canister/storage.rs (L149-149)
```rust
    pub static API_BOUNDARY_NODE_PRINCIPALS: RefCell<HashSet<Principal>> = RefCell::new(HashSet::new());
```

**File:** rs/boundary_node/rate_limits/canister/state.rs (L125-128)
```rust
    fn is_api_boundary_node_principal(&self, principal: &Principal) -> bool {
        self.api_boundary_node_principals
            .with(|cell| cell.borrow().contains(principal))
    }
```

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L130-139)
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
```

**File:** rs/boundary_node/rate_limits/canister/confidentiality_formatting.rs (L21-26)
```rust
        config.rules.iter_mut().for_each(|rule| {
            if rule.disclosed_at.is_none() {
                rule.description = None;
                rule.rule_raw = None;
            }
        });
```
