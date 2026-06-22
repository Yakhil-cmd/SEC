### Title
Uninitialized `API_BOUNDARY_NODE_PRINCIPALS` Set Causes Temporary DoS of `get_salt` After Every Canister Upgrade - (`rs/boundary_node/salt_sharing/canister/storage.rs`)

---

### Summary

The `salt_sharing_canister` stores its set of authorized API boundary node principals in a `thread_local!` heap variable (`API_BOUNDARY_NODE_PRINCIPALS`) that is initialized to an empty `HashSet` and is **never populated synchronously** during `canister_init` or `canister_post_upgrade`. Population only occurs after the first periodic timer fires — which is delayed by `registry_polling_interval_secs` seconds (e.g., 300 s on mainnet). Because heap memory is wiped on every upgrade, every upgrade resets this set to empty, causing all `get_salt` calls to fail for the entire polling interval window.

---

### Finding Description

**Storage initialization** (`rs/boundary_node/salt_sharing/canister/storage.rs`):

```rust
pub static API_BOUNDARY_NODE_PRINCIPALS: RefCell<HashSet<Principal>> =
    RefCell::new(HashSet::new());   // always starts empty
``` [1](#0-0) 

**`init` / `post_upgrade`** schedule `init_async` via a zero-duration timer (fires next round), but `init_async` only calls `set_timer_interval(period, ...)` — the **first** `poll_api_boundary_nodes()` fires after `period` seconds, not immediately:

```rust
#[init]
fn init(init_arg: InitArg) {
    set_timer(Duration::ZERO, async { init_async(init_arg).await });
    ...
}
#[post_upgrade]
fn post_upgrade(init_arg: InitArg) { init(init_arg); }
``` [2](#0-1) 

```rust
pub async fn init_async(init_arg: InitArg) {
    ...
    let period = Duration::from_secs(init_arg.registry_polling_interval_secs);
    set_timer_interval(period, async || poll_api_boundary_nodes().await);
    // ^^^ first poll fires only after `period` seconds — no immediate poll
}
``` [3](#0-2) 

**Authorization check** in both `inspect_message` (for update/replicated-query calls) and `get_salt` (for query calls) delegates to `is_api_boundary_node_principal`, which reads the empty set:

```rust
pub fn is_api_boundary_node_principal(principal: &Principal) -> bool {
    API_BOUNDARY_NODE_PRINCIPALS.with(|cell| cell.borrow().contains(principal))
}
``` [4](#0-3) 

```rust
fn inspect_message() {
    if called_method == REPLICATED_QUERY_METHOD && is_api_boundary_node_principal(&caller_id) {
        accept_message();
    } else {
        trap("message_inspection_failed: ...");
    }
}
``` [5](#0-4) 

```rust
fn get_salt() -> GetSaltResponse {
    if is_api_boundary_node_principal(&caller_id) { ... }
    Err(GetSaltError::Unauthorized)
}
``` [6](#0-5) 

**Population only happens after the timer fires:**

```rust
API_BOUNDARY_NODE_PRINCIPALS.with(|cell| *cell.borrow_mut() = principals);
``` [7](#0-6) 

---

### Impact Explanation

After every canister upgrade (a routine NNS governance operation), `API_BOUNDARY_NODE_PRINCIPALS` is reset to an empty `HashSet` because it lives in heap memory, not stable memory. For the entire `registry_polling_interval_secs` window (300 s on mainnet per the published proposal), every `get_salt` call — whether issued as a query or a replicated query — is rejected with `Unauthorized` or trapped by `inspect_message`. API boundary nodes that depend on the salt for rate-limiting or request-fingerprinting lose access to the salt for this window on every upgrade cycle. This is a direct analog to the VeQoda `_methods` uninitialized-set bug: a critical authorization set is not populated at initialization time, blocking the canister's primary operation. [8](#0-7) 

---

### Likelihood Explanation

Every canister upgrade resets heap state. Upgrades are routine governance operations. The mainnet polling interval is 300 s, so every upgrade produces a guaranteed 5-minute window during which all API boundary nodes are denied the salt. No attacker action is required; the condition is triggered by normal operations.

---

### Recommendation

Add an immediate call to `poll_api_boundary_nodes()` inside `init_async` **before** setting up the periodic timer, so that `API_BOUNDARY_NODE_PRINCIPALS` is populated in the same async task that runs right after init/upgrade:

```rust
pub async fn init_async(init_arg: InitArg) {
    // ... salt regeneration ...
    poll_api_boundary_nodes().await;   // <-- populate immediately
    let period = Duration::from_secs(init_arg.registry_polling_interval_secs);
    set_timer_interval(period, async || poll_api_boundary_nodes().await);
}
```

---

### Proof of Concept

1. Upgrade the `salt_sharing_canister` via NNS governance with any valid `InitArg`.
2. Immediately after the upgrade completes (before `registry_polling_interval_secs` elapses), call `get_salt` from any registered API boundary node principal.
3. Observe `GetSaltError::Unauthorized` (query path) or a trap from `inspect_message` (update path), because `API_BOUNDARY_NODE_PRINCIPALS` is empty.
4. Wait `registry_polling_interval_secs` seconds; the call now succeeds.

The integration test itself documents this behavior — the salt is not available immediately after installation and requires multiple rounds to tick before the timer fires: [9](#0-8)

### Citations

**File:** rs/boundary_node/salt_sharing/canister/storage.rs (L50-51)
```rust
    // Authorized principals allowed to retrieve the salt from the canister.
    pub static API_BOUNDARY_NODE_PRINCIPALS: RefCell<HashSet<Principal>> = RefCell::new(HashSet::new());
```

**File:** rs/boundary_node/salt_sharing/canister/canister.rs (L20-30)
```rust
#[inspect_message]
fn inspect_message() {
    let caller_id = caller();
    let called_method = method_name();

    if called_method == REPLICATED_QUERY_METHOD && is_api_boundary_node_principal(&caller_id) {
        accept_message();
    } else {
        trap("message_inspection_failed: method call is prohibited in the current context");
    }
}
```

**File:** rs/boundary_node/salt_sharing/canister/canister.rs (L33-50)
```rust
#[init]
fn init(init_arg: InitArg) {
    set_timer(Duration::ZERO, async { init_async(init_arg).await });
    // Update metric.
    let current_time = time() as i64;
    METRICS.with(|cell| {
        cell.borrow_mut()
            .last_canister_change_time
            .set(current_time);
    });
}

// Runs on every canister upgrade
#[post_upgrade]
fn post_upgrade(init_arg: InitArg) {
    // Run the same initialization logic
    init(init_arg);
}
```

**File:** rs/boundary_node/salt_sharing/canister/canister.rs (L52-66)
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
}
```

**File:** rs/boundary_node/salt_sharing/canister/helpers.rs (L22-35)
```rust
pub async fn init_async(init_arg: InitArg) {
    if (!is_salt_init() || init_arg.regenerate_now)
        && let Err(err) = try_regenerate_salt().await
    {
        log!(P0, "[init_regenerate_salt_failed]: {err}");
    }
    // Start salt generation schedule based on the argument.
    match init_arg.salt_generation_strategy {
        SaltGenerationStrategy::StartOfMonth => schedule_monthly_salt_generation(),
    }
    // Set up periodical job to get all API boundary node IDs from the registry.
    let period = Duration::from_secs(init_arg.registry_polling_interval_secs);
    set_timer_interval(period, async || poll_api_boundary_nodes().await);
}
```

**File:** rs/boundary_node/salt_sharing/canister/helpers.rs (L88-91)
```rust
        Ok((Ok(api_bn_records),)) => {
            // Set authorized readers of salt.
            let principals: HashSet<_> = api_bn_records.into_iter().filter_map(|n| n.id).collect();
            API_BOUNDARY_NODE_PRINCIPALS.with(|cell| *cell.borrow_mut() = principals);
```

**File:** rs/boundary_node/salt_sharing/canister/helpers.rs (L125-127)
```rust
pub fn is_api_boundary_node_principal(principal: &Principal) -> bool {
    API_BOUNDARY_NODE_PRINCIPALS.with(|cell| cell.borrow().contains(principal))
}
```

**File:** rs/boundary_node/salt_sharing/proposals/install_03-03-2025_135629.md (L52-58)
```markdown
    '(
        record {
            salt_generation_strategy = variant { StartOfMonth };
            regenerate_now = true;
            registry_polling_interval_secs = 300 : nat64;
        },
    )' | xxd -r -p | sha256sum
```

**File:** rs/boundary_node/salt_sharing/integration_tests/tests/salt_sharing_canister_tests.rs (L66-78)
```rust
    // Salt should not be initialized immediately after canister's installation
    let salt_id = metrics_extractor
        .try_get_metric::<u64>(SALT_METRIC)
        .await
        .unwrap();
    assert_eq!(salt_id, 0);
    // But once some rounds pass, salt should be initialized
    tick_n_times(&pocket_ic, TICKS).await;
    let salt_id = metrics_extractor
        .try_get_metric::<u64>(SALT_METRIC)
        .await
        .unwrap();
    assert!(salt_id > 0);
```
