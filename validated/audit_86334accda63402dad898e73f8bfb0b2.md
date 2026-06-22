Based on my investigation of the actual code, here is my analysis:

## Key Technical Facts

**`API_BOUNDARY_NODE_PRINCIPALS` is non-stable thread_local memory:**

In `state.rs`, `CanisterState` holds:
```rust
api_boundary_node_principals: LocalRef<HashSet<Principal>>,
```
backed by `&API_BOUNDARY_NODE_PRINCIPALS` — a `thread_local` `HashSet<Principal>`. [1](#0-0) 

**`set_api_boundary_nodes_principals` fully replaces the HashSet (not stable):** [2](#0-1) 

**`inspect_message` gates `get_config` on this HashSet:** [3](#0-2) 

**`post_upgrade` calls `init`, which only schedules a timer interval — not an immediate fire:** [4](#0-3) [5](#0-4) 

`set_timer_interval(interval, ...)` fires the first callback only after `interval` seconds, not at time zero.

---

### Title
Post-Upgrade Boundary Node Lockout via Non-Stable `API_BOUNDARY_NODE_PRINCIPALS` — (`rs/boundary_node/rate_limits/canister/canister.rs` + `state.rs`)

### Summary
After every canister upgrade, `API_BOUNDARY_NODE_PRINCIPALS` (a `thread_local` `HashSet`) is reset to empty. The periodic timer that repopulates it fires only after `registry_polling_period_secs` seconds. During this window, `inspect_message` evaluates `is_api_boundary_node_principal` against an empty set, causing it to trap with `"method call is prohibited"` for every `get_config` ingress call from a legitimate API boundary node.

### Finding Description
`API_BOUNDARY_NODE_PRINCIPALS` is declared as a `thread_local` (heap-allocated, non-stable) `HashSet<Principal>`. [6](#0-5) 

On canister upgrade, the IC runtime discards all heap state. `post_upgrade` → `init` → `periodically_poll_api_boundary_nodes` registers a `set_timer_interval` with the configured polling period, but the first callback fires only after that full interval elapses. [7](#0-6) 

Until the first timer fires and `set_api_boundary_nodes_principals` is called, `is_api_boundary_node_principal` always returns `false`. [8](#0-7) 

`inspect_message` then traps for any `get_config` call from a boundary node, since neither `has_full_access` nor `has_full_read_access` is true. [9](#0-8) 

### Impact Explanation
All API boundary nodes are locked out of `get_config` for the duration of `registry_polling_period_secs` after every canister upgrade. During this window, boundary nodes cannot fetch updated rate-limit configurations and continue enforcing stale rules. If a new rate-limit rule was deployed to block an active attack immediately before or during an upgrade, boundary nodes will fail to apply it for the entire lockout window.

### Likelihood Explanation
Canister upgrades are routine governance operations, observable on-chain. The lockout window is deterministic and equals `registry_polling_period_secs`. Any boundary node operator or external observer can confirm the window. The bug is reproducible on every upgrade.

### Recommendation
Either:
1. Store `API_BOUNDARY_NODE_PRINCIPALS` in stable memory (e.g., using `ic-stable-structures`), so it survives upgrades, **or**
2. Add an immediate one-shot timer (`set_timer(Duration::ZERO, ...)`) in `post_upgrade`/`init` to populate the set before the interval timer fires.

### Proof of Concept
1. Register a boundary node principal `BN_P`.
2. Upgrade the rate-limit canister.
3. Immediately (before `registry_polling_period_secs` elapses) send a `get_config` ingress message as `BN_P`.
4. Observe: `inspect_message` traps with `"message_inspection_failed: method call is prohibited in the current context"`.
5. Wait `registry_polling_period_secs`; repeat step 3 — call now succeeds.

The window of unavailability equals exactly `registry_polling_period_secs`.

### Citations

**File:** rs/boundary_node/rate_limits/canister/state.rs (L36-53)
```rust
pub struct CanisterState {
    configs: LocalRef<StableMap<StorableVersion, StorableConfig>>,
    rules: LocalRef<StableMap<StorableRuleId, StorableRule>>,
    incidents: LocalRef<StableMap<StorableIncidentId, StorableIncident>>,
    authorized_principal: LocalRef<StableMap<(), StorablePrincipal>>,
    api_boundary_node_principals: LocalRef<HashSet<Principal>>,
}

impl CanisterState {
    pub fn from_static() -> Self {
        Self {
            configs: &CONFIGS,
            rules: &RULES,
            incidents: &INCIDENTS,
            authorized_principal: &AUTHORIZED_PRINCIPAL,
            api_boundary_node_principals: &API_BOUNDARY_NODE_PRINCIPALS,
        }
    }
```

**File:** rs/boundary_node/rate_limits/canister/state.rs (L125-128)
```rust
    fn is_api_boundary_node_principal(&self, principal: &Principal) -> bool {
        self.api_boundary_node_principals
            .with(|cell| cell.borrow().contains(principal))
    }
```

**File:** rs/boundary_node/rate_limits/canister/state.rs (L130-133)
```rust
    fn set_api_boundary_nodes_principals(&self, principals: Vec<Principal>) {
        API_BOUNDARY_NODE_PRINCIPALS
            .with(|cell| *cell.borrow_mut() = HashSet::from_iter(principals));
    }
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

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L99-104)
```rust
// Run every time a canister is upgraded
#[post_upgrade]
fn post_upgrade(init_arg: InitArg) {
    // Run the same initialization logic
    init(init_arg);
}
```

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L251-255)
```rust
fn periodically_poll_api_boundary_nodes(interval: u64, canister_api: Arc<dyn CanisterApi>) {
    let interval = Duration::from_secs(interval);

    ic_cdk_timers::set_timer_interval(interval, move || {
        let canister_api = canister_api.clone();
```
