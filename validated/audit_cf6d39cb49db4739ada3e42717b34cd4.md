### Title
IP-Based `add_node` Rate Limiter State Lost on Canister Upgrade Due to Heap-Only Storage - (File: `rs/registry/canister/src/rate_limits.rs`)

### Summary
The Registry canister's IP-based rate limiter for `add_node` operations is stored exclusively in heap memory (`InMemoryRateLimiter`) via a `thread_local!` static. Unlike the node-operator and node-provider rate limiters — which use `StableMemoryCapacityStorage` backed by `MemoryId`-allocated stable memory — the IP limiter's state is wiped on every canister upgrade. An unprivileged node operator can exploit this by timing bulk `add_node` calls immediately after a registry canister upgrade, bypassing the intended 7-node-per-IP burst cap and flooding the registry with fraudulent node registrations.

### Finding Description
In `rs/registry/canister/src/rate_limits.rs`, three rate limiters are declared:

1. `NODE_PROVIDER_RATE_LIMITER` — backed by `StableMemoryCapacityStorage` at `MemoryId::new(2)` (persists across upgrades).
2. `NODE_OPERATOR_RATE_LIMITER` — backed by `StableMemoryCapacityStorage` at `MemoryId::new(3)` (persists across upgrades).
3. `ADD_NODE_IP_RATE_LIMITER` — backed by `InMemoryCapacityStorage` (heap only, **lost on upgrade**). [1](#0-0) 

The comment on line 59 explicitly acknowledges this: `"Stored in heap memory (not stable memory)."` The `InMemoryRateLimiter` type alias resolves to `RateLimiter<K, InMemoryCapacityStorage<K>>`, which holds all capacity data in a plain in-memory `BTreeMap`. [2](#0-1) 

The registry canister's `canister_pre_upgrade` serializes only the registry data itself to stable memory; it does not serialize the heap-resident IP rate limiter state. [3](#0-2) 

After `canister_post_upgrade`, the `ADD_NODE_IP_RATE_LIMITER` `thread_local` is re-initialized from scratch with full capacity (`max_capacity = ADD_NODE_IP_MAX_SPIKE = 7`), erasing all previously consumed tokens. [4](#0-3) 

The `do_add_node` function calls `try_reserve_add_node_capacity` before committing a node registration: [5](#0-4) 

The stable-memory-backed `NODE_OPERATOR_RATE_LIMITER` and `NODE_PROVIDER_RATE_LIMITER` are stored at dedicated `MemoryId`s in `rs/registry/canister/src/storage.rs`: [6](#0-5) 

No equivalent `MemoryId` exists for the IP rate limiter, confirming it has no stable-memory slot.

### Impact Explanation
An attacker who is a registered node operator (a role that requires only a governance proposal to obtain, not privileged access) can:

1. Monitor the NNS for a pending registry canister upgrade proposal.
2. Immediately after the upgrade executes, submit up to 7 `add_node` calls per IP address (the full burst capacity) before any capacity is consumed.
3. Rotate through multiple IP addresses (each gets a fresh 7-token bucket) to register an unbounded number of fraudulent node records in the registry.
4. Repeat after every subsequent upgrade.

This disrupts the IC node onboarding workflow: the registry is flooded with fake node entries, KYC/node-approval processes for legitimate operators are overwhelmed, and the `max_number_of_canisters`-style subnet capacity checks may be affected by inflated node counts. The impact is analogous to the reported bug: bulk fake registrations overloading the support/approval workflow.

**Impact: Medium** — Requires a registered node operator identity (not fully anonymous), but node operator registration is a governance-controlled process that is not secret or expensive. The attack window is every upgrade cycle.

### Likelihood Explanation
Registry canister upgrades occur regularly (multiple times per month, as evidenced by the CHANGELOG). Each upgrade resets the IP limiter. A node operator watching the NNS dashboard can trivially time the attack. The `do_add_node` endpoint is callable by any registered node operator principal with no additional authentication beyond holding the operator key. [7](#0-6) 

**Likelihood: Medium** — Requires a node operator identity and upgrade timing awareness, both of which are realistic for a motivated attacker.

### Recommendation
Migrate `ADD_NODE_IP_RATE_LIMITER` from `InMemoryRateLimiter` to `StableRateLimiter` backed by a new `MemoryId` (e.g., `MemoryId::new(4)`), following the same pattern used for `NODE_PROVIDER_RATE_LIMITER` and `NODE_OPERATOR_RATE_LIMITER`. Add the corresponding `get_add_node_ip_rate_limiter_memory()` accessor in `rs/registry/canister/src/storage.rs` and update the `thread_local!` declaration in `rs/registry/canister/src/rate_limits.rs` to use `RateLimiter::new_stable(...)`. [8](#0-7) [9](#0-8) 

### Proof of Concept

**Setup:** Attacker holds a registered node operator principal with `max_rewardable_nodes: {"type1": 100}`.

**Step 1 — Pre-upgrade baseline:** IP `1.2.3.4` has consumed 6 of 7 tokens. One more `add_node` call from this IP would be blocked.

**Step 2 — Registry upgrade executes** (e.g., NNS proposal passes). `canister_post_upgrade` runs; `ADD_NODE_IP_RATE_LIMITER` is re-initialized with `max_capacity = 7`, all prior consumption erased.

**Step 3 — Attacker submits 7 `add_node` calls** from IP `1.2.3.4` immediately after upgrade. All 7 succeed because the limiter starts at full capacity.

**Step 4 — Rotate IPs.** For each additional IP address, another 7 registrations succeed. With N IP addresses, the attacker registers 7×N fake nodes.

**Verification:** The test `test_ip_rate_limiting_for_add_node` confirms the limiter starts at capacity 7 and blocks at 8: [10](#0-9) 

Since `ADD_NODE_IP_RATE_LIMITER` is a `thread_local!` `InMemoryRateLimiter`, its state does not survive `canister_post_upgrade`, making the capacity reset after every upgrade a deterministic, exploitable condition. [11](#0-10)

### Citations

**File:** rs/registry/canister/src/rate_limits.rs (L29-31)
```rust
const AVG_ADD_NODE_BY_IP_PER_DAY: u64 = 1;
const ADD_NODE_IP_MAX_SPIKE: u64 = AVG_ADD_NODE_BY_IP_PER_DAY * 7;
const ADD_NODE_IP_REFILL_INTERVAL_SECONDS: u64 = ONE_DAY_SECONDS;
```

**File:** rs/registry/canister/src/rate_limits.rs (L33-56)
```rust
thread_local! {
    static NODE_PROVIDER_RATE_LIMITER: RefCell<
        RateLimiter<String, StableMemoryCapacityStorage<String, VM>>,
    > = RefCell::new(RateLimiter::new_stable(
        RateLimiterConfig {
            add_capacity_amount: 1,
            add_capacity_interval: Duration::from_secs(NODE_PROVIDER_CAPACITY_ADD_INTERVAL_SECONDS),
            max_capacity: NODE_PROVIDER_MAX_SPIKE,
            max_reservations: NODE_PROVIDER_MAX_SPIKE * 2,
        },
        get_node_provider_rate_limiter_memory(),
    ));

    static NODE_OPERATOR_RATE_LIMITER: RefCell<
        RateLimiter<String, StableMemoryCapacityStorage<String, VM>>,
    > = RefCell::new(RateLimiter::new_stable(
        RateLimiterConfig {
            add_capacity_amount: 1,
            add_capacity_interval: Duration::from_secs(NODE_OPERATOR_CAPACITY_ADD_INTERVAL_SECONDS),
            max_capacity: NODE_OPERATOR_MAX_SPIKE,
            max_reservations: NODE_OPERATOR_MAX_SPIKE * 2,
        },
        get_node_operator_rate_limiter_memory(),
    ));
```

**File:** rs/registry/canister/src/rate_limits.rs (L58-70)
```rust
    /// IP-based rate limiter for add_node operations.
    /// Stored in heap memory (not stable memory).
    /// Limits to 1 node addition per day per IP address.
    static ADD_NODE_IP_RATE_LIMITER: RefCell<InMemoryRateLimiter<String>> =
        RefCell::new(InMemoryRateLimiter::new_in_memory(
            RateLimiterConfig {
                add_capacity_amount: 1,
                add_capacity_interval: Duration::from_secs(ADD_NODE_IP_REFILL_INTERVAL_SECONDS),
                max_capacity: ADD_NODE_IP_MAX_SPIKE,
                max_reservations: ADD_NODE_IP_MAX_SPIKE * 2,
            },
        ));
}
```

**File:** rs/nervous_system/rate_limits/src/lib.rs (L162-168)
```rust
// Convenience type alias for the common in-memory case
pub type InMemoryRateLimiter<K> = RateLimiter<K, InMemoryCapacityStorage<K>>;

impl<K: Ord + Clone + Debug> InMemoryRateLimiter<K> {
    pub fn new_in_memory(config: RateLimiterConfig) -> Self {
        Self::new(config, InMemoryCapacityStorage::default())
    }
```

**File:** rs/registry/canister/canister/canister.rs (L240-250)
```rust
#[unsafe(export_name = "canister_pre_upgrade")]
fn canister_pre_upgrade() {
    println!("{LOG_PREFIX}canister_pre_upgrade");
    let registry = registry();
    let ss = RegistryCanisterStableStorage {
        registry: Some(registry.serializable_form()),
        pre_upgrade_version: Some(registry.latest_version()),
    };
    with_upgrades_memory(|memory| store_protobuf(memory, &ss))
        .expect("Failed to encode protobuf pre-upgrade");
}
```

**File:** rs/registry/canister/src/mutations/node_management/do_add_node.rs (L44-49)
```rust
    pub fn do_add_node(&mut self, payload: AddNodePayload) -> Result<NodeId, String> {
        // Get the caller ID and check if it is in the registry
        let caller_id = dfn_core::api::caller();
        println!("{LOG_PREFIX}do_add_node started: {payload:?} caller: {caller_id:?}");
        self.do_add_node_(payload, caller_id, now_system_time())
    }
```

**File:** rs/registry/canister/src/mutations/node_management/do_add_node.rs (L87-90)
```rust
        // 2a. Check IP-based rate limiting (1 node addition per day per IP)
        let ip_addr = http_endpoint.ip_addr.clone();
        let ip_reservation = try_reserve_add_node_capacity(now, ip_addr.clone())
            .map_err(|e| format!("{LOG_PREFIX}do_add_node: {e}"))?;
```

**File:** rs/registry/canister/src/mutations/node_management/do_add_node.rs (L1293-1326)
```rust
        // Check that we start with capacity available
        let initial_capacity = get_available_add_node_capacity(test_ip.clone(), now);
        assert_eq!(initial_capacity, 7, "Should start with 7 capacity");

        // Act: Add first node with a specific IP - should succeed
        let result_1 =
            registry.do_add_node_(add_node_payload_with_same_ip(1), node_operator_id, now);
        assert!(result_1.is_ok(), "First node addition should succeed");

        assert_eq!(get_available_add_node_capacity(test_ip.clone(), now), 6,);

        // The next 6 should also succeed.
        for i in 2..=7 {
            registry
                .do_add_node_(add_node_payload_with_same_ip(i), node_operator_id, now)
                .unwrap();
        }
        assert_eq!(
            get_available_add_node_capacity(test_ip.clone(), now),
            0,
            "Capacity should be exhausted after 7 nodes"
        );

        let new_payload = add_node_payload_with_same_ip(8);
        let result_2 = registry.do_add_node_(new_payload.clone(), node_operator_id, now);
        assert!(
            result_2.is_err(),
            "Second node addition should fail due to rate limiting"
        );
        let error_message = result_2.unwrap_err();
        assert!(
            error_message.contains("Capacity exceeded") || error_message.contains("Rate"),
            "Error message should mention rate/capacity limit, got: {error_message}"
        );
```

**File:** rs/registry/canister/src/storage.rs (L14-17)
```rust
const UPGRADES_MEMORY_ID: MemoryId = MemoryId::new(0);
const CHUNKS_MEMORY_ID: MemoryId = MemoryId::new(1);
const NODE_PROVIDER_RATE_LIMITER_MEMORY_ID: MemoryId = MemoryId::new(2);
const NODE_OPERATOR_RATE_LIMITER_MEMORY_ID: MemoryId = MemoryId::new(3);
```

**File:** rs/registry/canister/src/storage.rs (L72-80)
```rust
// Used to create the rate limiter
pub(crate) fn get_node_provider_rate_limiter_memory() -> VM {
    MEMORY_MANAGER.with(|mm| mm.borrow().get(NODE_PROVIDER_RATE_LIMITER_MEMORY_ID))
}

// Used to create the node operator rate limiter
pub fn get_node_operator_rate_limiter_memory() -> VM {
    MEMORY_MANAGER.with(|mm| mm.borrow().get(NODE_OPERATOR_RATE_LIMITER_MEMORY_ID))
}
```
