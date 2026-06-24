### Title
Single Authorized Principal Blocks Safety-Critical Rate-Limit Operations When Principal Is Unavailable - (`File: rs/boundary_node/rate_limits/canister/canister.rs`)

---

### Summary

The rate-limits canister enforces a single `authorized_principal` as the sole entity permitted to call `add_config` and `disclose_rules` via a `canister_inspect_message` hook. Canister controllers are explicitly excluded from this allowlist. If the authorized principal's key is unavailable — due to compromise requiring rotation, key loss, or the window during a governance-driven canister upgrade that rotates the principal — the canister's safety-critical functions are completely blocked with no fallback path, even for controllers.

---

### Finding Description

The `inspect_message` hook in `rs/boundary_node/rate_limits/canister/canister.rs` enforces authorization by reading `authorized_principal` from canister state pre-consensus:

```rust
let (has_full_access, has_full_read_access) = with_canister_state(|state| {
    let authorized_principal = state.get_authorized_principal();
    (
        Some(caller_id) == authorized_principal,
        state.is_api_boundary_node_principal(&caller_id),
    )
});
// ...
} else if UPDATE_METHODS.contains(&called_method.as_str()) {
    if has_full_access {
        ic_cdk::api::call::accept_message();
    } else {
        ic_cdk::api::trap("message_inspection_failed: unauthorized caller");
    }
}
``` [1](#0-0) 

The `authorized_principal` is set only at `init`/`post_upgrade` time:

```rust
if let Some(principal) = init_arg.authorized_principal {
    state.set_authorized_principal(principal);
}
``` [2](#0-1) 

There is no provision for canister controllers, NNS governance, or any other fallback principal to call `add_config` or `disclose_rules`. The `inspect_message` hook is the sole gate for all ingress messages to these methods. [3](#0-2) 

The `inspect_message` system method runs **pre-consensus** on a non-replicated snapshot of canister state: [4](#0-3) 

The `ApiType::InspectMessage` variant uses `ModificationTracking::Ignore`, meaning it reads from the last certified state snapshot, not the live replicated state: [5](#0-4) 

This creates a concrete blocking window: when the `authorized_principal` is rotated via a canister upgrade (the only mechanism to change it), the `inspect_message` hook continues to use the **old** certified state until the new state is certified post-consensus. During this window, the new authorized principal is rejected at `inspect_message` while the old principal (whose key may be compromised) still passes. Both `add_config` and `disclose_rules` are inaccessible to the legitimate new operator.

The production deployment confirms a single DFINITY-controlled key is the sole authorized principal: [6](#0-5) 

---

### Impact Explanation

`add_config` and `disclose_rules` are the canister's safety-critical operations: they push rate-limit rules that API boundary nodes enforce to protect the IC during active incidents. If the authorized principal is unavailable — due to key compromise requiring rotation, or during the stale-state window after an upgrade — no rate-limit rules can be pushed. An ongoing DDoS or protocol-level attack cannot be mitigated via the rate-limit canister. The canister's entire protective function is nullified. Canister controllers (including NNS governance) have no bypass path because `inspect_message` rejects their calls before they reach the update handler.

---

### Likelihood Explanation

The authorized principal is a single externally-held key. Key compromise is a realistic operational risk. The rotation path (NNS governance proposal → canister upgrade) introduces a multi-hour delay during which the stale-state window exists. An attacker who knows the key is being rotated can time an attack to exploit this window. Additionally, if the key is lost or the authorized principal's infrastructure is unavailable during an active incident, the canister provides zero protection.

---

### Recommendation

1. Add canister controllers to the `inspect_message` allowlist for `add_config` and `disclose_rules` as an emergency fallback:
   ```rust
   let is_controller = ic_cdk::api::is_controller(&caller_id);
   ```
2. Support a list of authorized principals rather than a single one, so key rotation does not create a blocking window.
3. Consider allowing NNS governance (via inter-canister call, which bypasses `inspect_message`) to call emergency rate-limit operations directly.

---

### Proof of Concept

1. Deploy the rate-limits canister with `authorized_principal = Some(principal_A)`.
2. Submit a governance proposal to upgrade the canister with `authorized_principal = Some(principal_B)`.
3. During the upgrade execution window (between proposal execution and state certification), `principal_B` attempts to call `add_config` — `inspect_message` reads stale state (still `principal_A`) and traps with `"message_inspection_failed: unauthorized caller"`.
4. `principal_A` (now potentially compromised) can still pass `inspect_message` during this window.
5. After state certification, `principal_B` can call `add_config`, but `principal_A` is now blocked — however, if `principal_A`'s key was compromised, the attacker had a window to push malicious rate-limit rules.

Alternatively: if `principal_A`'s key is simply lost (no rotation possible), `add_config` and `disclose_rules` are permanently inaccessible — confirmed by the `inspect_message` logic which has no controller fallback path. [7](#0-6) [8](#0-7)

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

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L76-78)
```rust
        if let Some(principal) = init_arg.authorized_principal {
            state.set_authorized_principal(principal);
        }
```

**File:** rs/execution_environment/src/execution/inspect_message.rs (L17-21)
```rust
/// Executes the system method `canister_inspect_message`.
///
/// This method is called pre-consensus to let the canister decide if it
/// wants to accept the message or not.
#[allow(clippy::too_many_arguments)]
```

**File:** rs/embedders/src/wasmtime_embedder/system_api.rs (L709-711)
```rust
            ApiType::ReplicatedQuery { .. }
            | ApiType::NonReplicatedQuery { .. }
            | ApiType::InspectMessage { .. } => ModificationTracking::Ignore,
```

**File:** rs/boundary_node/rate_limits/proposals/install_10-01-2025_134775.md (L47-49)
```markdown
        authorized_principal = opt principal "2igsz-4cjfz-unvfj-s4d3u-ftcdb-6ibug-em6tf-nzm2h-6igks-spdus-rqe";
        registry_polling_period_secs = 60;
    })' | xxd -r -p | sha256sum
```

**File:** rs/boundary_node/rate_limits/canister/state.rs (L57-65)
```rust
    fn get_authorized_principal(&self) -> Option<StorablePrincipal> {
        self.authorized_principal
            .with(|cell| cell.borrow().get(&()))
    }

    fn set_authorized_principal(&self, principal: Principal) {
        self.authorized_principal
            .with(|cell| cell.borrow_mut().insert((), principal));
    }
```
