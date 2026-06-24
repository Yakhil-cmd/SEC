### Title
API Boundary Node Principals Lost Across Canister Upgrades Due to Non-Stable Storage — (`rs/boundary_node/rate_limits/canister/storage.rs`, `canister.rs`, `state.rs`)

---

### Summary

`API_BOUNDARY_NODE_PRINCIPALS` is stored in a plain `thread_local! RefCell<HashSet<Principal>>` with no stable memory backing. On every canister upgrade, this set is wiped to `HashSet::new()`. The timer that repopulates it fires only after `registry_polling_period_secs` seconds. During this window, all API boundary nodes lose `FullRead` access and receive `RestrictedRead` responses, meaning confidential rate-limit rules are hidden from them and cannot be enforced.

---

### Finding Description

In `storage.rs`, all persistent state uses `StableBTreeMap` backed by `MemoryId`-allocated virtual memory: [1](#0-0) 

`CONFIGS`, `RULES`, `INCIDENTS`, and `AUTHORIZED_PRINCIPAL` all survive upgrades via stable memory slots 0–3. However, `API_BOUNDARY_NODE_PRINCIPALS` is declared as a plain heap-resident `RefCell<HashSet<Principal>>`: [2](#0-1) 

On upgrade, `post_upgrade` delegates directly to `init`: [3](#0-2) 

`init` calls `periodically_poll_api_boundary_nodes`, which registers a **timer interval** — it does not fire immediately: [4](#0-3) 

The timer fires after `registry_polling_period_secs` seconds. Until then, `API_BOUNDARY_NODE_PRINCIPALS` is empty. The `AccessLevelResolver` checks this set to grant `FullRead`: [5](#0-4) 

With an empty set, every boundary node principal falls through to `AccessLevel::RestrictedRead`. The `inspect_message` hook also uses this check to gate update-style calls to `get_config`: [6](#0-5) 

---

### Impact Explanation

During the post-upgrade window (up to `registry_polling_period_secs` seconds):

- **Query calls** to `get_config` by boundary nodes return `RestrictedRead` responses — confidential (not-yet-disclosed) rate-limit rules are redacted.
- **Update calls** to `get_config` by boundary nodes are rejected entirely by `inspect_message`.

Boundary nodes operating on stale or redacted rule sets cannot enforce confidential rate-limit rules. Traffic that should be rate-limited passes through unimpeded.

---

### Likelihood Explanation

NNS upgrade proposals for this canister are public and their execution time is observable on-chain. An unprivileged attacker who monitors governance proposals can time a burst of traffic to coincide with the upgrade execution window. No special privileges are required to exploit the window — the attacker simply sends ingress traffic through the boundary nodes during the gap. The window length is bounded by `registry_polling_period_secs` but is nonzero on every upgrade.

---

### Recommendation

Persist `API_BOUNDARY_NODE_PRINCIPALS` in stable memory using a `StableBTreeMap` with a new `MemoryId` (e.g., `MemoryId::new(4)`), mirroring the pattern used for `AUTHORIZED_PRINCIPAL`: [7](#0-6) 

Alternatively, in `post_upgrade`, immediately fire a one-shot timer with zero delay to populate the set before the interval timer is registered, so the window is minimized to a single async round-trip to the registry canister rather than a full polling interval.

---

### Proof of Concept

1. Install the canister with a known API boundary node principal `BN`.
2. Wait for the first registry poll to populate `API_BOUNDARY_NODE_PRINCIPALS` with `BN`.
3. Trigger a canister upgrade (via NNS proposal or in a local PocketIC test).
4. Immediately after upgrade completes, call `get_config` as `BN` **before** `registry_polling_period_secs` elapses.
5. Assert the response is `RestrictedRead` (confidential rules redacted) rather than `FullRead` (all rules visible).

The existing integration test at `rs/boundary_node/rate_limits/integration_tests/tests/rate_limit_canister_tests.rs` performs an upgrade at step 4 but does not assert boundary node access immediately post-upgrade before the timer fires — confirming the gap is untested. [8](#0-7)

### Citations

**File:** rs/boundary_node/rate_limits/canister/storage.rs (L21-24)
```rust
const MEMORY_ID_CONFIGS: MemoryId = MemoryId::new(0);
const MEMORY_ID_RULES: MemoryId = MemoryId::new(1);
const MEMORY_ID_INCIDENTS: MemoryId = MemoryId::new(2);
const MEMORY_ID_AUTHORIZED_PRINCIPAL: MemoryId = MemoryId::new(3);
```

**File:** rs/boundary_node/rate_limits/canister/storage.rs (L137-141)
```rust
    pub static AUTHORIZED_PRINCIPAL: RefCell<StableMap<(), StorablePrincipal>> = RefCell::new(
        StableMap::init(
            MEMORY_MANAGER.with(|m| m.borrow().get(MEMORY_ID_AUTHORIZED_PRINCIPAL)),
        )
    );
```

**File:** rs/boundary_node/rate_limits/canister/storage.rs (L149-149)
```rust
    pub static API_BOUNDARY_NODE_PRINCIPALS: RefCell<HashSet<Principal>> = RefCell::new(HashSet::new());
```

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L40-55)
```rust
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

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L84-88)
```rust
        // API boundary nodes are authorized readers of all config rules (including not yet disclosed ones)
        periodically_poll_api_boundary_nodes(
            init_arg.registry_polling_period_secs,
            Arc::new(state),
        );
```

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L100-104)
```rust
#[post_upgrade]
fn post_upgrade(init_arg: InitArg) {
    // Run the same initialization logic
    init(init_arg);
}
```

**File:** rs/boundary_node/rate_limits/canister/access_control.rs (L46-54)
```rust
        let has_full_read_access = self
            .canister_api
            .is_api_boundary_node_principal(&self.caller_id);

        if has_full_read_access {
            return AccessLevel::FullRead;
        }

        AccessLevel::RestrictedRead
```

**File:** rs/boundary_node/rate_limits/integration_tests/tests/rate_limit_canister_tests.rs (L128-153)
```rust
    // 4. Upgrade the canister wasm code, also setting the authorized principal to PRINCIPAL_2 (PRINCIPAL_1 is now unauthorized)
    let authorized_principal = Principal::from_text(PRINCIPAL_2).unwrap();
    let initial_payload = InitArg {
        authorized_principal: Some(authorized_principal),
        registry_polling_period_secs: 1,
    };
    let current_wasm_hash = get_installed_wasm_hash(&pocket_ic, canister_id).await;
    let new_wasm = modify_wasm_bytes(&wasm.clone().bytes(), 42);
    let new_wasm_hash = Sha256::hash(&new_wasm.clone());

    assert_ne!(current_wasm_hash, new_wasm_hash);

    pocket_ic
        .upgrade_canister(
            canister_id,
            new_wasm,
            Encode!(&initial_payload).unwrap(),
            None,
        )
        .await
        .unwrap();

    assert_eq!(
        get_installed_wasm_hash(&pocket_ic, canister_id).await,
        new_wasm_hash,
    );
```
