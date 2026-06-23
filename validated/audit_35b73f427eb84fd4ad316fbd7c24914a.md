### Title
`inspect_message` Authorization Gate Bypassed via Regular Query Call on `get_config` — (File: `rs/boundary_node/rate_limits/canister/canister.rs`)

---

### Summary
The rate-limits canister uses an `#[inspect_message]` hook to restrict access to `get_config` to only the authorized principal and registered API boundary nodes. However, `get_config` is declared as a `#[query]` method. The IC protocol never invokes `inspect_message` for regular (non-replicated) query calls. Any unprivileged caller can therefore invoke `get_config` as a plain query, completely bypassing the pre-consensus access gate. The handler falls back to `AccessLevel::RestrictedRead`, whose confidentiality formatter may still expose non-disclosed, operationally sensitive rate-limit rule data.

---

### Finding Description

The `inspect_message` hook in `rs/boundary_node/rate_limits/canister/canister.rs` obtains the caller with `ic_cdk::api::caller()` and enforces the following policy:

```
REPLICATED_QUERY_METHOD ("get_config") → accept only if has_full_access || has_full_read_access
UPDATE_METHODS ("add_config", "disclose_rules") → accept only if has_full_access
everything else → trap
``` [1](#0-0) 

The `get_config` handler is declared `#[query]`, not `#[update]`: [2](#0-1) 

On the Internet Computer, `inspect_message` is **only** invoked for ingress *update* messages before they enter consensus. It is never called for query calls (which are handled off-consensus by a single replica). Because `get_config` is a `#[query]` endpoint, the entire `inspect_message` gate is silently skipped when the method is called as a regular query.

The handler itself re-derives the caller with `ic_cdk::api::caller()` and constructs an `AccessLevelResolver`: [3](#0-2) 

For any caller that is neither the authorized principal nor a registered API boundary node, `AccessLevelResolver::get_access_level()` returns `AccessLevel::RestrictedRead`: [4](#0-3) 

The `ConfigConfidentialityFormatter` then filters the response according to that level. The `inspect_message` design intent is to **block** `RestrictedRead` callers from reaching `get_config` at all — yet the query path makes that gate unreachable, so the formatter is the only remaining control. If the formatter returns any non-public rule data under `RestrictedRead` (e.g., undisclosed rule bodies, incident IDs, or partial configuration), confidential operational security data is exposed to any anonymous caller.

---

### Impact Explanation

Rate-limit rules stored in this canister can be marked confidential (not yet disclosed). The `inspect_message` gate was designed to prevent callers without `FullRead` or `FullAccess` from retrieving the configuration at all. Because that gate is bypassed for query calls, an unprivileged caller receives whatever `ConfigConfidentialityFormatter` returns under `RestrictedRead`. Any confidential rule content surfaced at that level leaks to the public, potentially revealing DDoS-mitigation strategies, targeted IP ranges, or other operationally sensitive boundary-node configuration before operators intend to disclose it.

---

### Likelihood Explanation

The attack requires no special privilege: any principal (including the anonymous principal) can issue a standard query call to `get_config`. The IC HTTP interface exposes query calls to the public internet via boundary nodes. No key material, governance majority, or social engineering is required.

---

### Recommendation

1. **Move the authorization check into the handler itself.** Replace the `inspect_message`-only gate with an explicit check at the top of `get_config` that traps or returns an error for callers below `FullRead` access level. `inspect_message` can remain as an early-rejection optimization but must not be the sole enforcement point.

2. **Alternatively**, change `get_config` from `#[query]` to `#[update]` (replicated query semantics) so that `inspect_message` is actually invoked for every call. This matches the canister's own label `REPLICATED_QUERY_METHOD` and the original design intent.

---

### Proof of Concept

```
# Any unauthenticated caller issues a standard query call:
dfx canister call <rate_limits_canister_id> get_config '(null)' --query

# inspect_message is never invoked for --query calls.
# The handler runs, assigns AccessLevel::RestrictedRead, and returns
# whatever ConfigConfidentialityFormatter exposes at that level —
# potentially including undisclosed, confidential rate-limit rules.
``` [1](#0-0) [2](#0-1) [5](#0-4)

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

**File:** rs/boundary_node/rate_limits/canister/access_control.rs (L16-55)
```rust
#[derive(Clone, PartialEq, Eq)]
pub enum AccessLevel {
    FullAccess,
    FullRead,
    RestrictedRead,
}

#[derive(Clone)]
pub struct AccessLevelResolver<R: CanisterApi> {
    pub caller_id: Principal,
    pub canister_api: R,
}

impl<R: CanisterApi> AccessLevelResolver<R> {
    pub fn new(caller_id: Principal, canister_api: R) -> Self {
        Self {
            caller_id,
            canister_api,
        }
    }
}

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
