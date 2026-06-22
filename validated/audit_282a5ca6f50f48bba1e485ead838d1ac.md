### Title
Rate-Limit Canister `add_config` and `disclose_rules` Permanently Blocked When `authorized_principal` Is Not Set in `InitArg` - (File: `rs/boundary_node/rate_limits/canister/canister.rs`)

---

### Summary

The rate-limit canister's `init` function accepts an **optional** `authorized_principal` in `InitArg`. If it is omitted (`None`), no authorized principal is stored in stable state. The `inspect_message` hook then permanently rejects every ingress call to `add_config` and `disclose_rules`, and the `WithAuthorization` middleware also rejects any canister-to-canister call to those same methods. Because no update method exists to set the authorized principal post-deployment, the canister's entire write surface is rendered inoperable until a governance-approved upgrade is executed.

---

### Finding Description

**Root cause — optional principal never stored:**

In `rs/boundary_node/rate_limits/canister/canister.rs`, the `init` handler only stores the authorized principal when the caller explicitly provides one:

```rust
#[init]
fn init(init_arg: InitArg) {
    with_canister_state(|state| {
        if let Some(principal) = init_arg.authorized_principal {   // ← guarded by Option
            state.set_authorized_principal(principal);
        }
        ...
    });
}
``` [1](#0-0) 

When `authorized_principal` is `None`, `AUTHORIZED_PRINCIPAL` stable storage remains empty and `get_authorized_principal()` returns `None`. [2](#0-1) 

**First gate — `inspect_message` pre-consensus rejection:**

The `inspect_message` hook computes `has_full_access` as `Some(caller_id) == authorized_principal`. When `authorized_principal` is `None`, this expression is always `false` for every possible caller:

```rust
let (has_full_access, _) = with_canister_state(|state| {
    let authorized_principal = state.get_authorized_principal();
    (Some(caller_id) == authorized_principal, ...)
});
``` [3](#0-2) 

For the two write methods, the hook traps unconditionally:

```rust
} else if UPDATE_METHODS.contains(&called_method.as_str()) {
    if has_full_access {
        ic_cdk::api::call::accept_message();
    } else {
        ic_cdk::api::trap("message_inspection_failed: unauthorized caller");
    }
}
``` [4](#0-3) 

`UPDATE_METHODS` is `["add_config", "disclose_rules"]`. [5](#0-4) 

**Second gate — `WithAuthorization` middleware rejection:**

Even for canister-to-canister calls (which bypass `inspect_message`), the `WithAuthorization` wrapper inside `add_config` and `disclose_rules` calls `get_access_level()`. When `authorized_principal` is `None`, the `if let Some(...)` guard fails and the function returns `RestrictedRead`, not `FullAccess`:

```rust
fn get_access_level(&self) -> AccessLevel {
    if let Some(authorized_principal) = self.canister_api.get_authorized_principal()
        && self.caller_id == authorized_principal
    {
        return AccessLevel::FullAccess;
    }
    ...
    AccessLevel::RestrictedRead
}
``` [6](#0-5) 

Both `add_config` and `disclose_rules` then return `Err(AddConfigError::Unauthorized)` / `Err(DiscloseRulesError::Unauthorized)`. [7](#0-6) 

**No recovery path without an upgrade:**

There is no exposed update method to set the authorized principal. The only path to recovery is a governance-approved canister upgrade that passes `authorized_principal: Some(...)` in the `post_upgrade` argument. [8](#0-7) 

---

### Impact Explanation

While the canister is deployed with `authorized_principal: None`:

1. **`add_config` is completely blocked** — no new rate-limit configurations can be pushed to the canister. API boundary nodes therefore cannot receive updated rules and cannot enforce rate-limiting during an active incident.
2. **`disclose_rules` is completely blocked** — no rules can ever be made publicly visible.
3. **`get_config` as a replicated (update-path) query is blocked** for any caller that is neither the (absent) authorized principal nor a registered API boundary node — which is the case immediately after deployment before the periodic registry poll has populated the boundary-node set.

The canister's entire security purpose — protecting the IC from incidents via rate-limiting — is nullified for the duration of the deployment window.

---

### Likelihood Explanation

The `authorized_principal` field is typed as `Option<Principal>` in `InitArg`, making omission syntactically valid and easy to do accidentally. The system-test runbook at `rs/tests/boundary_nodes/rate_limit_canister_test.rs` explicitly exercises this exact scenario (Step 2: install with `authorized_principal: None`; Step 7: assert write call is rejected), confirming the path is reachable in practice. [9](#0-8) [10](#0-9) 

Any NNS proposal that installs or upgrades the rate-limit canister without supplying `authorized_principal` triggers the condition. The NNS governance process is the normal deployment path for this canister, so the risk is not hypothetical.

---

### Recommendation

Make `authorized_principal` a **required** field (remove the `Option` wrapper) in `InitArg`, or add a guard in `init` that traps if it is absent:

```rust
#[init]
fn init(init_arg: InitArg) {
    let principal = init_arg.authorized_principal
        .expect("authorized_principal must be set during initialization");
    with_canister_state(|state| {
        state.set_authorized_principal(principal);
        ...
    });
}
```

Alternatively, expose a dedicated `set_authorized_principal` update method callable only by the canister's NNS controller, so the principal can be set without a full upgrade.

---

### Proof of Concept

The system test at `rs/tests/boundary_nodes/rate_limit_canister_test.rs` is a self-contained proof of concept:

1. Install the rate-limit canister with `authorized_principal: None` (line 127).
2. Call `add_config` — the call is rejected with `"message_inspection_failed: unauthorized caller"` (line 235).
3. Upgrade the canister with `authorized_principal: Some(full_access_principal)` (lines 242–259).
4. Call `add_config` again — it now succeeds (line 295). [11](#0-10) [12](#0-11)

### Citations

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L30-31)
```rust
const UPDATE_METHODS: [&str; 2] = ["add_config", "disclose_rules"];
const REPLICATED_QUERY_METHOD: &str = "get_config";
```

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

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L72-78)
```rust
fn init(init_arg: InitArg) {
    let current_time = ic_cdk::api::time();
    with_canister_state(|state| {
        // Set authorized principal, which performs write operations, such as adding new configurations
        if let Some(principal) = init_arg.authorized_principal {
            state.set_authorized_principal(principal);
        }
```

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L100-104)
```rust
#[post_upgrade]
fn post_upgrade(init_arg: InitArg) {
    // Run the same initialization logic
    init(init_arg);
}
```

**File:** rs/boundary_node/rate_limits/canister/state.rs (L57-60)
```rust
    fn get_authorized_principal(&self) -> Option<StorablePrincipal> {
        self.authorized_principal
            .with(|cell| cell.borrow().get(&()))
    }
```

**File:** rs/boundary_node/rate_limits/canister/access_control.rs (L39-55)
```rust
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

**File:** rs/boundary_node/rate_limits/canister/access_control.rs (L72-98)
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

**File:** rs/tests/boundary_nodes/rate_limit_canister_test.rs (L230-235)
```rust
    info!(
        &logger,
        "Step 7. Assert canister call fails (rejected), as authorized_principal is unset for the rate-limit canister",
    );

    assert!(result.unwrap_err().contains("reject"));
```

**File:** rs/tests/boundary_nodes/rate_limit_canister_test.rs (L242-259)
```rust
    let args = Encode!(&InitArg {
        registry_polling_period_secs: 5,
        authorized_principal: Some(full_access_principal),
    })
    .unwrap();

    // apply a no-impact WASM modification and reinstall the canister
    let new_wasm = modify_wasm_bytes(wasm.bytes().as_slice(), 42);

    upgrade_nns_canister_by_proposal(
        &rate_limit_canister,
        &Canister::new(&nns, GOVERNANCE_CANISTER_ID),
        &root,
        true,
        Wasm::from_bytes(new_wasm),
        Some(args),
    )
    .await;
```
