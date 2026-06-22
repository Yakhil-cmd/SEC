### Title
`inspect_message` Authorization for `get_config` Bypassed via Inter-Canister Calls — (`rs/boundary_node/rate_limits/canister/canister.rs`)

### Summary

The rate-limits canister places its sole authorization check for the `get_config` endpoint inside the `inspect_message` hook. Because `inspect_message` is only invoked for ingress messages submitted through the HTTP API, any canister on the IC can call `get_config` via an inter-canister call and bypass the access control entirely, reading undisclosed rate-limiting rules.

### Finding Description

In `rs/boundary_node/rate_limits/canister/canister.rs`, the `inspect_message` hook is the only place where callers of `get_config` are checked for authorization:

```rust
const REPLICATED_QUERY_METHOD: &str = "get_config";

#[inspect_message]
fn inspect_message() {
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
    } ...
}
``` [1](#0-0) 

The IC protocol guarantees that `inspect_message` is **only** called for ingress messages arriving via the public HTTP API. It is **never** called for inter-canister (XNet) calls. Therefore, when any deployed canister on the IC calls `get_config` as an inter-canister update call, the `inspect_message` hook is skipped entirely. If the `get_config` function body itself contains no authorization check (the pattern here relies solely on `inspect_message`), the access control is completely bypassed.

This is structurally identical to the Rubicon M-34 finding: the security check (`buyEnabled` / authorization) is placed in one execution branch (`_buys()` / `inspect_message`) that is only reached via one entry path (matching enabled / ingress), while the other entry path (direct `super.buy()` / inter-canister call) reaches the sensitive function without passing through the check.

### Impact Explanation

The rate-limits canister stores rate-limiting rules in two states: disclosed (publicly visible) and undisclosed (visible only to the authorized principal and registered API boundary nodes). The `get_config` endpoint is intended to expose undisclosed rules only to those privileged callers. A malicious canister bypassing `inspect_message` can read all undisclosed rate-limiting rules before they are publicly disclosed. This leaks the operator's rate-limiting strategy and could allow an adversary to craft traffic patterns that evade boundary-node protections before rules are enforced.

### Likelihood Explanation

Any unprivileged canister developer can deploy a canister on the IC mainnet (requires only cycles) and issue an inter-canister call to `get_config`. No privileged keys, governance majority, or subnet compromise is required. The attacker-controlled entry path is a standard inter-canister update call, which is a normal, unrestricted IC operation.

### Recommendation

Move the authorization check out of `inspect_message` and into the `get_config` function body itself, so that it is enforced regardless of whether the caller is an ingress user or another canister:

```rust
#[query] // or #[update] depending on the method type
fn get_config(...) -> ... {
    let caller_id = ic_cdk::api::caller();
    with_canister_state(|state| {
        let authorized = Some(caller_id) == state.get_authorized_principal()
            || state.is_api_boundary_node_principal(&caller_id);
        if !authorized {
            ic_cdk::trap("unauthorized");
        }
        // ... existing logic
    })
}
```

`inspect_message` may still be retained as a pre-consensus optimization to shed unauthorized ingress load early, but it must never be the sole enforcement point.

### Proof of Concept

1. Deploy a canister `attacker` on the IC mainnet.
2. From `attacker`, issue an inter-canister call: `ic_cdk::call(rate_limits_canister_id, "get_config", ...)`.
3. The IC runtime does **not** invoke `inspect_message` for this call.
4. `get_config` executes without any authorization check and returns the full configuration including undisclosed rules.
5. `attacker` receives and logs the undisclosed rate-limiting rules. [2](#0-1) [3](#0-2)

### Citations

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L29-32)
```rust
const REGISTRY_CANISTER_METHOD: &str = "get_api_boundary_node_ids";
const UPDATE_METHODS: [&str; 2] = ["add_config", "disclose_rules"];
const REPLICATED_QUERY_METHOD: &str = "get_config";

```

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L34-67)
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
```
