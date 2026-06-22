All the relevant code is now confirmed. Here is the complete analysis:

---

### Title
Heap-Only `API_BOUNDARY_NODE_PRINCIPALS` Wiped on Upgrade Creates FullRead→RestrictedRead Downgrade Window — (`rs/boundary_node/rate_limits/canister/storage.rs`, `canister.rs`)

### Summary
`API_BOUNDARY_NODE_PRINCIPALS` is stored exclusively in heap memory (`thread_local! RefCell<HashSet<Principal>>`). On every canister upgrade, this set is silently reset to an empty `HashSet`. The `post_upgrade` hook re-registers a periodic timer to repopulate it, but `set_timer_interval` does not fire immediately — it fires only after the first full interval elapses. During that window, every API boundary node principal is treated as `RestrictedRead`, causing confidential (non-disclosed) `rule_raw` and `description` fields to be redacted from `get_config` and `get_rule_by_id` responses.

### Finding Description

**Root cause 1 — heap-only storage:**

`API_BOUNDARY_NODE_PRINCIPALS` is declared as a plain `RefCell<HashSet<Principal>>` with no backing stable memory: [1](#0-0) 

All other security-critical maps (`CONFIGS`, `RULES`, `INCIDENTS`, `AUTHORIZED_PRINCIPAL`) are backed by `StableBTreeMap` and survive upgrades. This one does not.

**Root cause 2 — timer fires after interval, not immediately:**

`post_upgrade` delegates to `init`, which calls `periodically_poll_api_boundary_nodes`: [2](#0-1) [3](#0-2) 

`ic_cdk_timers::set_timer_interval(interval, ...)` schedules the first execution after `interval` seconds — there is no immediate one-shot poll on startup. Until that first tick, the set remains empty.

**Root cause 3 — access level degrades to `RestrictedRead`:**

`is_api_boundary_node_principal` consults the now-empty set and returns `false`: [4](#0-3) 

`get_access_level` therefore falls through to `RestrictedRead`: [5](#0-4) 

**Root cause 4 — confidential fields are redacted for `RestrictedRead`:**

In `ConfigGetter::get`, when `is_authorized_viewer` is `false`, the formatter is applied: [6](#0-5) 

The formatter sets `is_redacted = true` and nulls `rule_raw`/`description` for every non-disclosed rule: [7](#0-6) 

The same redaction applies in `RuleGetter` and `IncidentGetter`: [8](#0-7) 

**Additional impact — replicated-query path is fully blocked:**

For ingress calls to `get_config` (the `REPLICATED_QUERY_METHOD`), `inspect_message` checks `has_full_read_access`. With an empty set this is `false`, so the call is trapped entirely: [9](#0-8) 

Boundary nodes therefore receive either a trap (replicated query path) or a fully-redacted response (non-replicated query path) for the entire duration of the upgrade window.

### Impact Explanation
Confidential rate-limit rules — those with `disclosed_at = None` — have their `rule_raw` payload nulled out in every response served to boundary nodes during the upgrade window. Without `rule_raw`, a boundary node cannot parse or enforce those rules. Any traffic that those rules were designed to rate-limit passes through unchecked for the duration of the window (bounded by `registry_polling_period_secs`, which could be tens of seconds to several minutes depending on deployment configuration).

### Likelihood Explanation
Every routine canister upgrade triggers the window unconditionally. No special attacker action is required beyond sending traffic during a known or observable upgrade event. The window length is determined by the polling interval, which is a deployment parameter visible in the `InitArg`.

### Recommendation
1. **Immediate fix**: Replace the heap-only `HashSet` with a stable-memory-backed structure (e.g., a `StableBTreeMap<StorablePrincipal, ()>`) so the set survives upgrades, consistent with how `AUTHORIZED_PRINCIPAL` is stored.
2. **Defense-in-depth**: Add a one-shot `set_timer(Duration::ZERO, ...)` in `post_upgrade`/`init` to trigger an immediate registry poll before the first interval fires, so the window is reduced to a single async round-trip rather than a full polling interval.

### Proof of Concept
Integration test outline:
1. Install the canister with a known API boundary node principal `P` and at least one non-disclosed rule.
2. Call `get_config` from `P`; assert `is_redacted = false` and `rule_raw` is present.
3. Upgrade the canister (before the timer fires).
4. Immediately call `get_config` from `P` again (before the polling interval elapses).
5. Assert `is_redacted = true` and `rule_raw = None` — confirming the downgrade window. [1](#0-0) [2](#0-1)

### Citations

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

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L100-104)
```rust
#[post_upgrade]
fn post_upgrade(init_arg: InitArg) {
    // Run the same initialization logic
    init(init_arg);
}
```

**File:** rs/boundary_node/rate_limits/canister/canister.rs (L251-254)
```rust
fn periodically_poll_api_boundary_nodes(interval: u64, canister_api: Arc<dyn CanisterApi>) {
    let interval = Duration::from_secs(interval);

    ic_cdk_timers::set_timer_interval(interval, move || {
```

**File:** rs/boundary_node/rate_limits/canister/state.rs (L125-128)
```rust
    fn is_api_boundary_node_principal(&self, principal: &Principal) -> bool {
        self.api_boundary_node_principals
            .with(|cell| cell.borrow().contains(principal))
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

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L130-147)
```rust
        let is_authorized_viewer = self.access_resolver.get_access_level()
            == AccessLevel::FullAccess
            || self.access_resolver.get_access_level() == AccessLevel::FullRead;

        if is_authorized_viewer {
            return Ok(api::ConfigResponse {
                version,
                active_since: stored_config.active_since,
                config: config.into(),
            });
        }

        // Hide non-disclosed rules from unauthorized viewers.
        Ok(api::ConfigResponse {
            version,
            active_since: stored_config.active_since,
            config: self.formatter.format(config).into(),
        })
```

**File:** rs/boundary_node/rate_limits/canister/getter.rs (L234-244)
```rust
        let is_authorized_viewer = self.access_resolver.get_access_level()
            == AccessLevel::FullAccess
            || self.access_resolver.get_access_level() == AccessLevel::FullRead;

        if is_authorized_viewer {
            return Ok(output_rule.into());
        }

        // Hide non-disclosed rules from unauthorized viewers.
        let output_rule = self.formatter.format(output_rule);

```

**File:** rs/boundary_node/rate_limits/canister/confidentiality_formatting.rs (L17-28)
```rust
    fn format(&self, config: OutputConfig) -> OutputConfig {
        let mut config = config;
        config.is_redacted = true;
        // Redact (hide) fields of non-disclosed rules
        config.rules.iter_mut().for_each(|rule| {
            if rule.disclosed_at.is_none() {
                rule.description = None;
                rule.rule_raw = None;
            }
        });
        config
    }
```
