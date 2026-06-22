### Title
Heap-Allocated `API_BOUNDARY_NODE_PRINCIPALS` Wiped on Every Upgrade Creates Salt-Distribution Outage Window — (`rs/boundary_node/salt_sharing/canister/storage.rs`)

---

### Summary

Every canister upgrade resets `API_BOUNDARY_NODE_PRINCIPALS` to an empty `HashSet` because it lives in heap memory, not stable memory. Re-population via `poll_api_boundary_nodes()` is deferred by the full registry polling interval. During this window every `get_salt()` call — including those from legitimate boundary nodes — returns `Unauthorized`, breaking salt distribution for all boundary nodes until the first successful registry poll completes.

---

### Finding Description

**Root cause — heap storage, not stable storage**

`API_BOUNDARY_NODE_PRINCIPALS` is declared as a plain `thread_local!` `RefCell<HashSet<Principal>>`: [1](#0-0) 

Heap memory is discarded on every upgrade. `SALT`, by contrast, is backed by a `StableBTreeMap` and survives upgrades: [2](#0-1) 

**Upgrade path**

`post_upgrade` delegates directly to `init`: [3](#0-2) 

`init` schedules `init_async` as a `Duration::ZERO` timer — it does not run synchronously: [4](#0-3) 

**`init_async` does not immediately populate the principal set**

`init_async` only registers a periodic interval timer; it does not call `poll_api_boundary_nodes()` directly: [5](#0-4) 

The first actual `poll_api_boundary_nodes()` execution is deferred by the full `registry_polling_interval_secs` after `init_async` itself runs (which is already one round after the upgrade). The principals set remains empty for that entire interval.

**Authorization check reads the empty set**

`get_salt()` is a `#[query]` method. Query calls bypass `inspect_message` entirely. The only guard is the inline check against the now-empty set: [6](#0-5) 

`is_api_boundary_node_principal` reads directly from the heap `HashSet`: [7](#0-6) 

Because the set is empty, every caller — including every legitimate boundary node — receives `GetSaltError::Unauthorized` for the duration of the window.

**Observable via public metrics**

`recompute_metrics()` reads the same heap set and exposes its size as `api_boundary_nodes_count` through the public `/metrics` HTTP endpoint: [8](#0-7) 

Any unauthenticated caller can query `/metrics` immediately after an upgrade and observe `api_boundary_nodes_count == 0`, confirming the window is open.

---

### Impact Explanation

During every upgrade, all boundary nodes lose the ability to retrieve the salt for the duration of the registry polling interval. Without the salt, boundary nodes cannot anonymize user traffic. The outage is total (all boundary nodes simultaneously), deterministic (triggered by every upgrade), and lasts for the full polling interval.

---

### Likelihood Explanation

The canister is expected to be upgraded periodically. Every upgrade unconditionally triggers the outage window. No special attacker action is required; the upgrade itself is the trigger. The window length equals `registry_polling_interval_secs`, which is a deployment-time argument and could be tens of seconds to minutes.

---

### Recommendation

Persist `API_BOUNDARY_NODE_PRINCIPALS` in stable memory (e.g., a `StableBTreeMap` under a dedicated `MemoryId`), mirroring how `SALT` is stored. Alternatively, call `poll_api_boundary_nodes()` synchronously (or schedule it with `Duration::ZERO` as a separate timer) inside `init_async` so the set is populated before the first round of query traffic is served post-upgrade.

---

### Proof of Concept

State-machine / PocketIC test outline:

1. Install the canister with a known boundary-node principal `P` and a `registry_polling_interval_secs` of, say, 60.
2. Advance time until `poll_api_boundary_nodes()` has run at least once; confirm `P` can call `get_salt()` successfully.
3. Upgrade the canister (same wasm, same `InitArg`).
4. **Immediately** (before advancing time by 60 s) call `http_request('/metrics')` and assert `api_boundary_nodes_count == 0`.
5. Call `get_salt()` from principal `P` and assert the response is `Err(GetSaltError::Unauthorized)`.
6. Advance time by `registry_polling_interval_secs`; confirm `P` can call `get_salt()` successfully again.

Steps 4 and 5 demonstrate the invariant violation: a previously-authorized principal is denied access immediately after a routine upgrade.

### Citations

**File:** rs/boundary_node/salt_sharing/canister/storage.rs (L45-48)
```rust
    // A cryptographic salt stored in stable memory to ensure persistence across upgrades.
    pub static SALT: RefCell<StableMap<(), StorableSalt>> = RefCell::new(
        StableMap::init(MEMORY_MANAGER.with(|m| m.borrow().get(MEMORY_ID_SALT)))
    );
```

**File:** rs/boundary_node/salt_sharing/canister/storage.rs (L50-51)
```rust
    // Authorized principals allowed to retrieve the salt from the canister.
    pub static API_BOUNDARY_NODE_PRINCIPALS: RefCell<HashSet<Principal>> = RefCell::new(HashSet::new());
```

**File:** rs/boundary_node/salt_sharing/canister/canister.rs (L34-35)
```rust
fn init(init_arg: InitArg) {
    set_timer(Duration::ZERO, async { init_async(init_arg).await });
```

**File:** rs/boundary_node/salt_sharing/canister/canister.rs (L46-49)
```rust
#[post_upgrade]
fn post_upgrade(init_arg: InitArg) {
    // Run the same initialization logic
    init(init_arg);
```

**File:** rs/boundary_node/salt_sharing/canister/canister.rs (L52-65)
```rust
#[query]
fn get_salt() -> GetSaltResponse {
    let caller_id = caller();
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

**File:** rs/boundary_node/salt_sharing/canister/helpers.rs (L32-34)
```rust
    // Set up periodical job to get all API boundary node IDs from the registry.
    let period = Duration::from_secs(init_arg.registry_polling_interval_secs);
    set_timer_interval(period, async || poll_api_boundary_nodes().await);
```

**File:** rs/boundary_node/salt_sharing/canister/helpers.rs (L125-127)
```rust
pub fn is_api_boundary_node_principal(principal: &Principal) -> bool {
    API_BOUNDARY_NODE_PRINCIPALS.with(|cell| cell.borrow().contains(principal))
}
```

**File:** rs/boundary_node/salt_sharing/canister/metrics.rs (L107-108)
```rust
        let api_bns_count = API_BOUNDARY_NODE_PRINCIPALS.with(|cell| cell.borrow().len());
        cell.api_boundary_nodes_count.set(api_bns_count as i64);
```
