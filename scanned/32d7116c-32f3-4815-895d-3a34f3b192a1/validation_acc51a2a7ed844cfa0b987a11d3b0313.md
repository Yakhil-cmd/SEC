### Title
Stale `API_BOUNDARY_NODE_PRINCIPALS` Cache Allows Removed Boundary Nodes to Retain Unauthorized Access and Causes Post-Upgrade Access Denial - (`rs/boundary_node/rate_limits/canister/canister.rs`, `rs/boundary_node/salt_sharing/canister/canister.rs`)

---

### Summary

The rate-limit canister and salt-sharing canister both maintain an in-memory (non-stable) cache of authorized API boundary node principals (`API_BOUNDARY_NODE_PRINCIPALS`). This cache is populated by a periodic timer that polls the registry canister. The cache is never atomically synchronized with registry changes: when an API boundary node is removed from the registry, the cache is not immediately cleared, and after a canister upgrade the cache is reset to empty. This creates two distinct cache-inconsistency windows that mirror the NestedFactory bug class exactly.

---

### Finding Description

Both canisters store the set of authorized API boundary node principals in a `thread_local!` `RefCell<HashSet<Principal>>` that lives in Wasm heap memory (not stable memory):

**Rate-limit canister** — `rs/boundary_node/rate_limits/canister/state.rs`:
```rust
api_boundary_node_principals: LocalRef<HashSet<Principal>>,
``` [1](#0-0) 

The cache is populated only by a periodic timer registered in `init`/`post_upgrade`: [2](#0-1) 

The timer uses `set_timer_interval`, which fires **after** the first full interval (e.g., 60 seconds on mainnet), not immediately: [3](#0-2) 

The `inspect_message` hook and `get_config` authorization both read from this cache: [4](#0-3) 

**Salt-sharing canister** — `rs/boundary_node/salt_sharing/canister/helpers.rs`:
```rust
API_BOUNDARY_NODE_PRINCIPALS.with(|cell| *cell.borrow_mut() = principals);
``` [5](#0-4) 

The `get_salt` query and `inspect_message` hook both read from this same non-stable cache: [6](#0-5) [7](#0-6) 

**Two concrete inconsistency instances:**

**Instance 1 — Removed node retains access (direct analog to NestedFactory `removeOperator()` bug):**
When an API boundary node is removed from the registry via `remove_api_boundary_nodes`, the registry is updated immediately: [8](#0-7) 

But `API_BOUNDARY_NODE_PRINCIPALS` in both canisters is only refreshed on the next timer tick. The removed node's principal remains in the cache for up to `registry_polling_period_secs` seconds (60 s on mainnet). During this window, the removed node passes the `is_api_boundary_node_principal()` check and can call `get_config` (rate-limit canister) and `get_salt` (salt-sharing canister).

**Instance 2 — Post-upgrade empty cache (analog to NestedFactory `addOperator()` bug):**
Because `API_BOUNDARY_NODE_PRINCIPALS` is heap memory (not stable memory), it is reset to an empty `HashSet` on every canister upgrade. The `post_upgrade` hook calls `init`, which re-registers the timer interval but does **not** perform an immediate poll: [9](#0-8) 

For the rate-limit canister, the first poll fires after the full `registry_polling_period_secs` interval. During this window, `is_api_boundary_node_principal()` returns `false` for every caller, so the `inspect_message` hook rejects all `get_config` update calls from legitimate API boundary nodes.

---

### Impact Explanation

**Instance 1 (stale access):**
- A decommissioned or compromised API boundary node that has been removed from the registry via NNS governance can still call `get_config` on the rate-limit canister to read **confidential, not-yet-disclosed rate-limit rules** (incident details kept private until disclosure). It can also call `get_salt` on the salt-sharing canister to retrieve the shared privacy salt, undermining the privacy guarantees of the rate-limiting system (enabling cross-node user deanonymization).
- The window is bounded by `registry_polling_period_secs` (60 s on mainnet for the rate-limit canister).

**Instance 2 (post-upgrade denial):**
- After every canister upgrade, legitimate API boundary nodes cannot call `get_config` as a replicated query (update call) for up to 60 seconds. This disrupts the ability of boundary nodes to obtain certified, up-to-date rate-limit rules immediately after an upgrade, creating a gap in rate-limit enforcement.

---

### Likelihood Explanation

- **Instance 1**: Realistic. API boundary nodes are operated by independent parties. A node removal via NNS proposal (e.g., due to compromise or decommissioning) is a normal operational event. The 60-second window is sufficient for a compromised node to make targeted calls.
- **Instance 2**: Certain. Every canister upgrade triggers the empty-cache window. Upgrades are routine governance operations.

Neither instance requires a subnet majority, threshold key compromise, or any privileged role beyond being (or having been) an API boundary node. The attacker-controlled entry path is a direct canister call from the removed boundary node's principal to the rate-limit or salt-sharing canister.

---

### Recommendation

1. **Immediate poll on init/post_upgrade**: In the rate-limit canister, replace `set_timer_interval` with an initial `set_timer(Duration::ZERO, ...)` followed by a recurring interval, mirroring the salt-sharing canister's `init_async` pattern. This eliminates the post-upgrade empty-cache window.

2. **Persist the principal set to stable memory**: Store `API_BOUNDARY_NODE_PRINCIPALS` in stable memory (e.g., using `ic_stable_structures`) so it survives upgrades without requiring a fresh poll.

3. **Immediate cache invalidation on removal**: Expose an authenticated endpoint (callable only by the registry canister or governance) that allows the registry to push removal notifications directly to the rate-limit and salt-sharing canisters, eliminating the polling-window stale-access problem.

---

### Proof of Concept

**Instance 1 (stale access after node removal):**

1. API boundary node `BN_A` is registered in the registry. The rate-limit canister polls and adds `BN_A`'s principal to `API_BOUNDARY_NODE_PRINCIPALS`.
2. An NNS governance proposal calls `remove_api_boundary_nodes({node_ids: [BN_A]})` on the registry canister. The registry is updated immediately.
3. Before the next timer tick (within 60 seconds), `BN_A` calls `get_config` on the rate-limit canister as an update call. The `inspect_message` hook checks `is_api_boundary_node_principal(BN_A)` — returns `true` (stale cache). The call is accepted and `BN_A` reads confidential rate-limit rules.
4. `BN_A` calls `get_salt` on the salt-sharing canister. `is_api_boundary_node_principal(BN_A)` returns `true` (stale cache). `BN_A` reads the shared salt.

**Instance 2 (post-upgrade denial):**

1. The rate-limit canister is upgraded via NNS proposal. `post_upgrade` calls `init`, which calls `periodically_poll_api_boundary_nodes(60, ...)`. `API_BOUNDARY_NODE_PRINCIPALS` is now empty.
2. Within the next 60 seconds, a legitimate API boundary node `BN_B` calls `get_config` as an update call. The `inspect_message` hook checks `is_api_boundary_node_principal(BN_B)` — returns `false` (empty cache). The call is rejected with `"message_inspection_failed: method call is prohibited in the current context"`.
3. After 60 seconds, the timer fires, polls the registry, and repopulates `API_BOUNDARY_NODE_PRINCIPALS`. `BN_B` can now call `get_config` successfully.

### Citations

**File:** rs/boundary_node/rate_limits/canister/state.rs (L41-41)
```rust
    api_boundary_node_principals: LocalRef<HashSet<Principal>>,
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

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L83-88)
```rust
        // Spawn periodic job of fetching latest API boundary node topology
        // API boundary nodes are authorized readers of all config rules (including not yet disclosed ones)
        periodically_poll_api_boundary_nodes(
            init_arg.registry_polling_period_secs,
            Arc::new(state),
        );
```

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L100-103)
```rust
#[post_upgrade]
fn post_upgrade(init_arg: InitArg) {
    // Run the same initialization logic
    init(init_arg);
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

**File:** rs/boundary_node/salt_sharing/canister/helpers.rs (L90-91)
```rust
            let principals: HashSet<_> = api_bn_records.into_iter().filter_map(|n| n.id).collect();
            API_BOUNDARY_NODE_PRINCIPALS.with(|cell| *cell.borrow_mut() = principals);
```

**File:** rs/boundary_node/salt_sharing/canister/canister.rs (L25-29)
```rust
    if called_method == REPLICATED_QUERY_METHOD && is_api_boundary_node_principal(&caller_id) {
        accept_message();
    } else {
        trap("message_inspection_failed: method call is prohibited in the current context");
    }
```

**File:** rs/boundary_node/salt_sharing/canister/canister.rs (L55-65)
```rust
    if is_api_boundary_node_principal(&caller_id) {
        let stored_salt = SALT
            .with(|cell| cell.borrow().get(&()))
            .ok_or(GetSaltError::SaltNotInitialized)?;

        return Ok(SaltResponse {
            salt: stored_salt.salt,
            salt_id: stored_salt.salt_id,
        });
    }
    Err(GetSaltError::Unauthorized)
```
