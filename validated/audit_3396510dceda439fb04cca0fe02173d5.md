### Title
Unguarded `authorized_principal` Overwrite in `post_upgrade` Allows Any Canister Controller to Hijack Rate-Limit Write Access — (File: `rs/boundary_node/rate_limits/canister/canister.rs`)

---

### Summary

The rate-limit canister's `post_upgrade` handler unconditionally calls `init`, which overwrites the stored `authorized_principal` with whatever principal is supplied in the upgrade argument — with no check that the caller is a legitimate governance-controlled upgrader. Any entity that can submit a canister upgrade (i.e., any current controller of the canister) can silently replace the authorized principal with an arbitrary one, granting themselves full write access (`add_config`, `disclose_rules`) to the rate-limit canister.

---

### Finding Description

`post_upgrade` in the rate-limit canister simply delegates to `init`:

```rust
// rs/boundary_node/rate_limits/canister/canister.rs, lines 100-104
#[post_upgrade]
fn post_upgrade(init_arg: InitArg) {
    // Run the same initialization logic
    init(init_arg);
}
```

`init` unconditionally overwrites the stored `authorized_principal` with whatever value is in `init_arg`:

```rust
// rs/boundary_node/rate_limits/canister/canister.rs, lines 71-97
#[init]
fn init(init_arg: InitArg) {
    ...
    with_canister_state(|state| {
        if let Some(principal) = init_arg.authorized_principal {
            state.set_authorized_principal(principal);   // ← no caller check
        }
        ...
    });
}
```

`set_authorized_principal` writes directly to stable storage:

```rust
// rs/boundary_node/rate_limits/canister/state.rs, lines 62-65
fn set_authorized_principal(&self, principal: Principal) {
    self.authorized_principal
        .with(|cell| cell.borrow_mut().insert((), principal));
}
```

The `authorized_principal` is the sole gate for `add_config` and `disclose_rules`. The `inspect_message` hook enforces this:

```rust
// rs/boundary_node/rate_limits/canister/canister.rs, lines 40-46
let (has_full_access, has_full_read_access) = with_canister_state(|state| {
    let authorized_principal = state.get_authorized_principal();
    (
        Some(caller_id) == authorized_principal,
        state.is_api_boundary_node_principal(&caller_id),
    )
});
```

There is no check inside `init` or `post_upgrade` that the caller is the NNS governance canister or any other trusted upgrader. The IC protocol itself enforces that only a controller can call `InstallCode`/upgrade, but the canister's own application logic performs no additional validation of *which* controller is performing the upgrade or *what* principal they are installing as the new authorized writer.

---

### Impact Explanation

The `authorized_principal` controls who can call `add_config` and `disclose_rules`. These are the two privileged write methods of the rate-limit canister, which is a production NNS canister that governs rate-limiting rules enforced by all API boundary nodes on the Internet Computer.

An attacker who gains controller status over the rate-limit canister (e.g., through a compromised NNS root or a governance proposal that adds a malicious controller) can upgrade the canister with an `InitArg` containing their own principal as `authorized_principal`. After the upgrade, they can:

1. Push arbitrary rate-limit rules via `add_config`, blocking or throttling any canister or user on the IC.
2. Disclose or suppress incident-related rules via `disclose_rules`.

This is a **governance authorization bypass** — the application-level access control for the most sensitive write operations can be silently redirected without any on-chain evidence beyond the upgrade transaction itself.

---

### Likelihood Explanation

The rate-limit canister is an NNS canister. Its controllers are NNS root and governance. A direct external attacker cannot upgrade it without a governance proposal. However:

- The vulnerability class is real and reachable: any entity that legitimately or illegitimately holds controller status can exploit it.
- The design intent (as shown in the integration test and the deployment proposal) is that `authorized_principal` should only be set by a trusted governance-controlled upgrade. The code provides no enforcement of this intent at the canister level.
- The analog vulnerability (missing init access control) is exactly the pattern flagged in the external report: the initialization function sets a privileged role without verifying the caller's identity.

Likelihood is **medium** — it requires controller access, but the absence of an application-level guard means the canister's security relies entirely on the IC's controller list remaining uncompromised, with no defense-in-depth.

---

### Recommendation

Inside `init` (and therefore `post_upgrade`), verify that the caller is the expected governance or root canister before overwriting `authorized_principal`:

```rust
fn init(init_arg: InitArg) {
    let caller = ic_cdk::api::caller();
    assert!(
        ic_cdk::api::is_controller(&caller),
        "Only a controller may initialize the authorized principal"
    );
    // ... rest of init
}
```

Alternatively, restrict `authorized_principal` changes to a dedicated, separately guarded update method (callable only by governance), and remove the ability to set it via upgrade arguments entirely.

---

### Proof of Concept

1. Attacker obtains controller status on the rate-limit canister (e.g., via a governance proposal adding their principal as a controller).
2. Attacker calls `InstallCode` (upgrade mode) with:
   ```
   InitArg {
       authorized_principal: Some(attacker_principal),
       registry_polling_period_secs: 60,
   }
   ```
3. `post_upgrade` → `init` → `state.set_authorized_principal(attacker_principal)` executes with no caller check.
4. Attacker now calls `add_config` with arbitrary rate-limit rules; `inspect_message` resolves `has_full_access = true` for the attacker's principal and accepts the message.
5. All API boundary nodes fetch and enforce the attacker-injected rules. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L100-104)
```rust
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
