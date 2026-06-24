Audit Report

## Title
IP-Based Add-Node Rate Limiter Resets on Every Registry Canister Upgrade — (`rs/registry/canister/src/rate_limits.rs`)

## Summary
`ADD_NODE_IP_RATE_LIMITER` is backed by `InMemoryCapacityStorage<String>` (a heap `BTreeMap`) rather than `StableMemoryCapacityStorage`. Because the IC canister runtime discards heap state on every upgrade, the per-IP token bucket is silently re-initialized to full capacity (7 slots) after each NNS-approved registry canister upgrade. A node operator can exhaust the 7-slot IP budget, wait for any routine upgrade, and immediately exhaust it again — indefinitely.

## Finding Description
`ADD_NODE_IP_RATE_LIMITER` is declared as a `thread_local!` static using `InMemoryRateLimiter<String>`, which wraps `InMemoryCapacityStorage<String>` — a plain heap `BTreeMap`: [1](#0-0) [2](#0-1) 

By contrast, `NODE_PROVIDER_RATE_LIMITER` and `NODE_OPERATOR_RATE_LIMITER` both use `StableMemoryCapacityStorage` backed by `MemoryId::new(2)` and `MemoryId::new(3)` respectively, which survive upgrades: [3](#0-2) [4](#0-3) 

No `MemoryId` is allocated for the IP rate limiter. `canister_post_upgrade` restores only the registry changelog from stable storage and contains no logic to restore IP rate limiter state: [5](#0-4) 

`do_add_node_` calls `try_reserve_add_node_capacity` (IP check) before applying mutations. After any upgrade, the heap bucket is fresh and the IP reservation always succeeds regardless of prior usage: [6](#0-5) 

## Impact Explanation
The IP rate limit is designed to enforce "≤1 node addition per day on average, max burst of 7" per IP address. After every registry canister upgrade the entire IP-keyed state is lost. A node operator can exhaust the 7-slot IP budget, observe the next NNS-approved registry upgrade (a public, on-chain event), and immediately submit 7 more `add_node` calls from the same IP — all succeeding. The node-operator and node-provider rate limiters (stable memory, unaffected by upgrades) still apply, so the total daily node-addition rate is bounded by those limits, but the per-IP invariant is fully broken across upgrade boundaries. This constitutes a **Medium** impact: a meaningful security control in the NNS registry canister is rendered ineffective, allowing faster-than-intended node registration from a single IP, but the attack is constrained by the requirement for a valid NNS-approved node operator record and the surviving operator/provider rate limits.

## Likelihood Explanation
Registry canister upgrades are routine NNS governance events occurring regularly. The attacker needs only a valid node operator record (NNS-approved) and knowledge of when an upgrade occurs (observable on-chain). No key compromise, social engineering, or threshold attack is required. The attack is repeatable on every subsequent upgrade.

## Recommendation
Allocate a new `MemoryId` (e.g. `MemoryId::new(4)`) in `rs/registry/canister/src/storage.rs` and back `ADD_NODE_IP_RATE_LIMITER` with `StableMemoryCapacityStorage` using that memory, exactly as is done for `NODE_PROVIDER_RATE_LIMITER` and `NODE_OPERATOR_RATE_LIMITER`: [4](#0-3) [7](#0-6) 

## Proof of Concept
The `get_available_add_node_capacity` helper is already present gated under `#[cfg(test)]`: [8](#0-7) 

A state-machine or PocketIC integration test can prove the issue deterministically:
1. Create a node operator record with sufficient `max_rewardable_nodes`.
2. Call `do_add_node_` 7 times with `http_endpoint` IP = `"1.2.3.4"` at time `T`. All 7 succeed; `get_available_add_node_capacity("1.2.3.4", T) == 0`.
3. Simulate `canister_post_upgrade` (re-initialize `Registry` from stable storage). The `ADD_NODE_IP_RATE_LIMITER` `thread_local` is re-initialized to a fresh `InMemoryCapacityStorage` (empty `BTreeMap`).
4. Call `get_available_add_node_capacity("1.2.3.4", T)` again. Returns `7` (full capacity restored), proving the reset.
5. Call `do_add_node_` 7 more times with the same IP. All 7 succeed, violating the ≤1/day-average invariant.

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

**File:** rs/registry/canister/src/rate_limits.rs (L61-69)
```rust
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

**File:** rs/registry/canister/src/rate_limits.rs (L221-224)
```rust
#[cfg(test)]
pub fn get_available_add_node_capacity(ip_addr: String, now: SystemTime) -> u64 {
    with_add_node_ip_rate_limiter(|rate_limiter| rate_limiter.get_available_capacity(ip_addr, now))
}
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

**File:** rs/registry/canister/src/storage.rs (L14-17)
```rust
const UPGRADES_MEMORY_ID: MemoryId = MemoryId::new(0);
const CHUNKS_MEMORY_ID: MemoryId = MemoryId::new(1);
const NODE_PROVIDER_RATE_LIMITER_MEMORY_ID: MemoryId = MemoryId::new(2);
const NODE_OPERATOR_RATE_LIMITER_MEMORY_ID: MemoryId = MemoryId::new(3);
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

**File:** rs/registry/canister/src/mutations/node_management/do_add_node.rs (L87-90)
```rust
        // 2a. Check IP-based rate limiting (1 node addition per day per IP)
        let ip_addr = http_endpoint.ip_addr.clone();
        let ip_reservation = try_reserve_add_node_capacity(now, ip_addr.clone())
            .map_err(|e| format!("{LOG_PREFIX}do_add_node: {e}"))?;
```
