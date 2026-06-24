The code evidence is conclusive. Let me lay out the exact findings.

**Key facts from the code:**

1. `ADD_NODE_IP_RATE_LIMITER` is explicitly heap-allocated: [1](#0-0) 

2. The other two rate limiters use `StableMemoryCapacityStorage` (survives upgrades): [2](#0-1) 

3. `canister_post_upgrade` restores only registry data from stable storage — it does not restore `ADD_NODE_IP_RATE_LIMITER` state: [3](#0-2) 

4. `do_add_node_` calls `try_reserve_add_node_capacity` using the in-memory limiter: [4](#0-3) 

5. `InMemoryCapacityStorage` is a plain `BTreeMap` in heap memory — wiped on every Wasm module re-instantiation (i.e., every upgrade): [5](#0-4) 

---

### Title
IP-Based `add_node` Rate Limiter State Lost on Canister Upgrade — (`rs/registry/canister/src/rate_limits.rs`)

### Summary
`ADD_NODE_IP_RATE_LIMITER` stores its per-IP capacity usage in heap memory (`InMemoryRateLimiter<String>` backed by `BTreeMap`). On every NNS-approved registry canister upgrade, the Wasm heap is wiped and the `thread_local!` static is re-initialized to full capacity (7). The operator/provider rate limiters use `StableMemoryCapacityStorage` and survive upgrades; the IP limiter does not. Any registered node operator can exhaust the IP limit, wait for a routine upgrade, then immediately exhaust it again — indefinitely repeating this cycle on every upgrade.

### Finding Description
In `rs/registry/canister/src/rate_limits.rs` lines 61–69, `ADD_NODE_IP_RATE_LIMITER` is declared as:

```rust
static ADD_NODE_IP_RATE_LIMITER: RefCell<InMemoryRateLimiter<String>> =
    RefCell::new(InMemoryRateLimiter::new_in_memory(...));
```

The comment even acknowledges: *"Stored in heap memory (not stable memory)."* On canister upgrade, `canister_post_upgrade` only deserializes `RegistryCanisterStableStorage` (the registry key-value log). There is no code path that serializes or restores `ADD_NODE_IP_RATE_LIMITER` state. After upgrade, every IP's available capacity is reset to `ADD_NODE_IP_MAX_SPIKE = 7`.

By contrast, `NODE_PROVIDER_RATE_LIMITER` and `NODE_OPERATOR_RATE_LIMITER` use `StableMemoryCapacityStorage` backed by `StableBTreeMap`, so their state persists across upgrades. [2](#0-1) 

### Impact Explanation
A registered node operator controlling a single IP can:
- Register 7 nodes from that IP (exhausting the spike limit)
- Wait for any routine NNS-approved registry canister upgrade (these occur regularly)
- Immediately register 7 more nodes from the same IP

This bypasses the invariant that a single IP may add at most 1 node per day on average (max burst 7). Over multiple upgrade cycles, an operator can register an unbounded number of nodes from a single IP in a short window, flooding the unassigned node pool with entries from one address. The operator/provider rate limits (stable memory, 20/day avg, 140 max spike) still apply, but the IP-level constraint — which exists specifically to prevent single-IP concentration — is fully defeated.

### Likelihood Explanation
Registry canister upgrades are routine NNS governance operations. The attacker does not need to trigger the upgrade — they only need to observe it (the registry version increments are publicly visible). The exploit requires only a registered node operator identity, which is a normal unprivileged role. No key compromise, no majority corruption, no social engineering is needed.

### Recommendation
Migrate `ADD_NODE_IP_RATE_LIMITER` to use `StableMemoryCapacityStorage` (as the operator and provider limiters already do), allocating a dedicated virtual memory region via the memory manager in `storage.rs`. Alternatively, serialize and restore the in-memory state explicitly in `canister_pre_upgrade`/`canister_post_upgrade`.

### Proof of Concept
State-machine test outline:
1. Register a node operator with a node provider.
2. Call `do_add_node_` 7 times from IP `1.2.3.4` — all succeed; 8th call returns `NotEnoughCapacity`.
3. Simulate upgrade: drop and re-create the `Registry` struct (heap reset), call `canister_post_upgrade` with the serialized stable storage.
4. Assert `get_available_add_node_capacity("1.2.3.4", now) == 7` — capacity is fully restored.
5. Call `do_add_node_` 7 more times from `1.2.3.4` — all succeed, demonstrating the bypass.

### Citations

**File:** rs/registry/canister/src/rate_limits.rs (L34-56)
```rust
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

**File:** rs/registry/canister/src/rate_limits.rs (L58-69)
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
```

**File:** rs/registry/canister/src/registry_lifecycle.rs (L19-81)
```rust
pub fn canister_post_upgrade(
    registry: &mut Registry,
    registry_storage: RegistryCanisterStableStorage,
) {
    // Purposefully fail the upgrade if we can't find authz information.
    // Best to have a broken canister, which we can reinstall, than a
    // canister without authz information.

    registry.from_serializable_form(
        registry_storage
            .registry
            .expect("Error decoding from stable"),
    );

    // Registry data migrations should be implemented as follows:
    let mutation_batches_due_to_data_migrations = {
        let mut total_batches = 0;

        let mutations = fix_node_operators_corrupted(registry);
        if !mutations.is_empty() {
            registry.maybe_apply_mutation_internal(mutations);
            total_batches += 1;
        }

        let mutations = fix_vetkd_pre_signatures_field(registry);
        if !mutations.is_empty() {
            registry.maybe_apply_mutation_internal(mutations);
            total_batches += 1;
        }

        let mutations = convert_type1dot1_nodes_to_type4dot5(registry);
        if !mutations.is_empty() {
            registry.maybe_apply_mutation_internal(mutations);
            total_batches += 1;
        }

        total_batches
    };
    //
    // When there are no migrations, `mutation_batches_due_to_data_migrations` should be set to `0`.
    // let mutation_batches_due_to_data_migrations = 0;

    registry.check_global_state_invariants(&[]);
    // Registry::from_serializable_from guarantees this always passes in this function
    // because it fills in missing versions to maintain that invariant
    registry.check_changelog_version_invariants();

    // This is no-op outside Canister environment, and is therefore not under unit-test coverage
    recertify_registry(registry);

    // ANYTHING BELOW THIS LINE SHOULD NOT MUTATE STATE

    if let Some(pre_upgrade_version) = registry_storage.pre_upgrade_version {
        assert_eq!(
            pre_upgrade_version + mutation_batches_due_to_data_migrations,
            registry.latest_version(),
            "The serialized last version watermark doesn't match what's found in the records. \
                     Watermark: {:?}, Last version: {:?}",
            pre_upgrade_version,
            registry.latest_version()
        );
    }
}
```

**File:** rs/registry/canister/src/mutations/node_management/do_add_node.rs (L88-90)
```rust
        let ip_addr = http_endpoint.ip_addr.clone();
        let ip_reservation = try_reserve_add_node_capacity(now, ip_addr.clone())
            .map_err(|e| format!("{LOG_PREFIX}do_add_node: {e}"))?;
```

**File:** rs/nervous_system/rate_limits/src/lib.rs (L51-67)
```rust
pub struct InMemoryCapacityStorage<K> {
    capacity_usage_records: BTreeMap<K, CapacityUsageRecord>,
}

impl<K: Ord + Clone> InMemoryCapacityStorage<K> {
    pub fn new() -> Self {
        Self::default()
    }
}

impl<K: Ord + Clone> Default for InMemoryCapacityStorage<K> {
    fn default() -> Self {
        Self {
            capacity_usage_records: Default::default(),
        }
    }
}
```
