### Title
Missing Anonymous-Principal Check When Setting `authorized_principal` in `init`/`post_upgrade` of the Rate-Limit Canister - (File: `rs/boundary_node/rate_limits/canister/canister.rs`)

---

### Summary

The rate-limit canister's `init` and `post_upgrade` functions accept an `authorized_principal` from the caller-supplied `InitArg` and store it without validating that it is not the anonymous principal. The `inspect_message` hook grants full write access to any caller whose identity matches the stored `authorized_principal`. If the canister is initialized or upgraded with `authorized_principal = Some(Principal::anonymous())`, every anonymous ingress message passes the authorization check and can inject arbitrary rate-limit rules that are enforced by all API boundary nodes on the IC.

---

### Finding Description

In `rs/boundary_node/rate_limits/canister/canister.rs`, the `inspect_message` hook compares the ingress caller against the stored authorized principal:

```rust
let (has_full_access, _) = with_canister_state(|state| {
    let authorized_principal = state.get_authorized_principal();
    (
        Some(caller_id) == authorized_principal,   // ← grants access if equal
        ...
    )
});
``` [1](#0-0) 

When `has_full_access` is `true`, the message is accepted for the privileged `add_config` and `disclose_rules` update methods:

```rust
} else if UPDATE_METHODS.contains(&called_method.as_str()) {
    if has_full_access {
        ic_cdk::api::call::accept_message();
    } else {
        ic_cdk::api::trap("message_inspection_failed: unauthorized caller");
    }
}
``` [2](#0-1) 

The `init` function stores whatever principal is supplied in `InitArg.authorized_principal` with no validation:

```rust
#[init]
fn init(init_arg: InitArg) {
    ...
    if let Some(principal) = init_arg.authorized_principal {
        state.set_authorized_principal(principal);   // ← no anonymous check
    }
    ...
}
``` [3](#0-2) 

`post_upgrade` delegates to the same `init` function, so the same gap applies on every upgrade: [4](#0-3) 

`set_authorized_principal` in `state.rs` also performs no validation before writing to stable storage:

```rust
fn set_authorized_principal(&self, principal: Principal) {
    self.authorized_principal
        .with(|cell| cell.borrow_mut().insert((), principal));
}
``` [5](#0-4) 

The `InitArg` type is defined in the public API and accepts any `Option<Principal>`, including `Principal::anonymous()`. There is no layer between the Candid-decoded value and the storage write that would reject the anonymous principal.

---

### Impact Explanation

If `authorized_principal` is set to `Principal::anonymous()`:

1. `Some(caller_id) == authorized_principal` evaluates to `true` for every anonymous ingress message.
2. `inspect_message` calls `accept_message()` for `add_config` and `disclose_rules`.
3. Any unauthenticated user on the internet can call `add_config` and push arbitrary rate-limit rules.
4. API boundary nodes periodically fetch and enforce these rules against all IC canisters.
5. An attacker can inject rules that block all traffic to any canister (including NNS canisters), causing a network-wide denial of service at the boundary layer.

The impact is a **boundary/API validation bypass** leading to unauthorized injection of rate-limit rules enforced across the entire IC boundary node fleet.

---

### Likelihood Explanation

The likelihood is **low but non-negligible**:

- The `InitArg` type is a plain Candid record; nothing in the type system prevents `Principal::anonymous()` from being supplied.
- The deployment runbook (as seen in `rs/tests/boundary_nodes/rate_limit_canister_test.rs`) explicitly demonstrates installing the canister with `authorized_principal: None` first and then upgrading to set it — a two-step process where a mistake in the upgrade argument is realistic.
- A malicious insider with upgrade authority (e.g., a compromised DFINITY key used to submit an NNS upgrade proposal) could deliberately set the anonymous principal to open the canister to public writes.
- The external report's scenario (deployment error or malicious insider) maps directly to this path. [6](#0-5) 

---

### Recommendation

**Short term**: Add an explicit anonymous-principal guard in `init`/`post_upgrade` before calling `set_authorized_principal`:

```rust
if let Some(principal) = init_arg.authorized_principal {
    if principal == Principal::anonymous() {
        ic_cdk::api::trap("authorized_principal must not be the anonymous principal");
    }
    state.set_authorized_principal(principal);
}
```

**Long term**: Add the same guard inside `set_authorized_principal` in `state.rs` so that no code path — present or future — can store the anonymous principal as the authorized writer. Consider adding a property-based test that verifies the invariant: the stored `authorized_principal` is never `Principal::anonymous()` after any sequence of `init`/`post_upgrade` calls.

---

### Proof of Concept

1. Deploy the rate-limit canister with:
   ```
   InitArg {
       authorized_principal: Some(Principal::anonymous()),
       registry_polling_period_secs: 60,
   }
   ```
2. Send an anonymous ingress `add_config` call with a rule that blocks all requests to a target canister.
3. Observe that `inspect_message` accepts the call (`Some(anonymous) == Some(anonymous)` → `has_full_access = true`).
4. The rule is stored and subsequently fetched and enforced by all API boundary nodes, blocking traffic to the targeted canister for every user on the IC. [1](#0-0) [7](#0-6)

### Citations

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L40-46)
```rust
    let (has_full_access, has_full_read_access) = with_canister_state(|state| {
        let authorized_principal = state.get_authorized_principal();
        (
            Some(caller_id) == authorized_principal,
            state.is_api_boundary_node_principal(&caller_id),
        )
    });
```

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L56-61)
```rust
    } else if UPDATE_METHODS.contains(&called_method.as_str()) {
        if has_full_access {
            ic_cdk::api::call::accept_message();
        } else {
            ic_cdk::api::trap("message_inspection_failed: unauthorized caller");
        }
```

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L71-97)
```rust
#[init]
fn init(init_arg: InitArg) {
    let current_time = ic_cdk::api::time();
    with_canister_state(|state| {
        // Set authorized principal, which performs write operations, such as adding new configurations
        if let Some(principal) = init_arg.authorized_principal {
            state.set_authorized_principal(principal);
        }
        // Initialize config only on the very first invocation
        if state.get_version().is_none() {
            init_version_and_config(current_time, state.clone());
        }
        // Spawn periodic job of fetching latest API boundary node topology
        // API boundary nodes are authorized readers of all config rules (including not yet disclosed ones)
        periodically_poll_api_boundary_nodes(
            init_arg.registry_polling_period_secs,
            Arc::new(state),
        );
    });
    // Update metric.
    METRICS.with(|cell| {
        let mut cell = cell.borrow_mut();
        cell.last_canister_change_time
            .borrow_mut()
            .set(current_time as i64);
    });
}
```

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L99-104)
```rust
// Run every time a canister is upgraded
#[post_upgrade]
fn post_upgrade(init_arg: InitArg) {
    // Run the same initialization logic
    init(init_arg);
}
```

**File:** rs/boundary_node/rate_limits/canister/state.rs (L62-65)
```rust
    fn set_authorized_principal(&self, principal: Principal) {
        self.authorized_principal
            .with(|cell| cell.borrow_mut().insert((), principal));
    }
```

**File:** rs/tests/boundary_nodes/rate_limit_canister_test.rs (L125-136)
```rust
    let args = Encode!(&InitArg {
        registry_polling_period_secs: 5,
        authorized_principal: None,
    })
    .unwrap();

    info!(
        &logger,
        "Installing rate-limit canister wasm (with unset authorized_principal)..."
    );

    install_rust_canister_from_path(&mut rate_limit_canister, path_to_wasm, Some(args)).await;
```
