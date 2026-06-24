Looking at the code carefully to determine if this is a real vulnerability.

### Title
IP-Based `add_node` Rate Limiter Resets on Canister Upgrade, Allowing >7 Node Registrations Per IP Per Day — (`rs/registry/canister/src/rate_limits.rs`)

---

### Summary

The `ADD_NODE_IP_RATE_LIMITER` is stored in heap (thread-local) memory and is not persisted across canister upgrades. Because the registry canister is upgraded periodically via governance proposals as part of normal IC operations, a registered node operator can exhaust the 7-node-per-IP burst limit, wait for any legitimate upgrade, and then register additional nodes from the same IP — violating the stated invariant.

---

### Finding Description

In `rs/registry/canister/src/rate_limits.rs`, three rate limiters are declared:

- `NODE_PROVIDER_RATE_LIMITER` — uses `StableMemoryCapacityStorage` (stable memory, survives upgrades)
- `NODE_OPERATOR_RATE_LIMITER` — uses `StableMemoryCapacityStorage` (stable memory, survives upgrades)
- `ADD_NODE_IP_RATE_LIMITER` — uses `InMemoryRateLimiter` (heap memory, **resets on upgrade**)

The code even self-documents this asymmetry: [1](#0-0) 

The `canister_pre_upgrade` hook saves only `RegistryCanisterStableStorage` (registry data + version watermark): [2](#0-1) 

No IP rate limiter state is serialized or restored. After `canister_post_upgrade`, the `ADD_NODE_IP_RATE_LIMITER` `thread_local!` is re-initialized from scratch with full capacity (`max_capacity = ADD_NODE_IP_MAX_SPIKE = 7`).

In `do_add_node_`, both the stable-memory operator limiter and the heap-based IP limiter are checked: [3](#0-2) 

After an upgrade, the operator/provider limiters (stable) retain their consumed capacity, but the IP limiter is fully reset — allowing the same IP to register 7 more nodes immediately.

---

### Impact Explanation

A registered node operator can register up to 7 nodes from a single IP, wait for any routine registry canister upgrade (which DFINITY deploys regularly), and then register 7 more nodes from the same IP. Repeating this across multiple upgrade cycles allows accumulating far more than 7 nodes per IP, undermining the Sybil-resistance guarantee of the IP-based rate limit. This could enable a single physical machine or IP to appear as many distinct nodes, influencing subnet membership composition.

The node operator rate limiter (stable memory, 20/day average, 140 burst) and `max_rewardable_nodes` quota still apply, so the bypass is bounded by those limits — but the specific IP-based invariant is broken.

---

### Likelihood Explanation

Registry canister upgrades are routine and happen on a cadence of weeks or less. The attacker does not need to trigger the upgrade — they only need to wait. The attack requires being a registered node operator (a semi-privileged but not governance-level role). The exploit is deterministic and local-testable.

---

### Recommendation

Migrate `ADD_NODE_IP_RATE_LIMITER` to use `StableMemoryCapacityStorage` backed by a dedicated stable memory region (as is already done for the operator and provider limiters), so that IP-based rate limit state survives canister upgrades. Allocate a new `MemoryId` in the memory manager for this purpose.

---

### Proof of Concept

State-machine test outline:

1. Register a node operator.
2. Call `do_add_node` 7 times from the same IP — all succeed; capacity reaches 0.
3. Assert the 8th call fails with a rate limit error.
4. Simulate a canister upgrade: serialize state via `canister_pre_upgrade`, reinitialize, call `canister_post_upgrade`.
5. Assert `get_available_add_node_capacity(same_ip, now) == 7` (reset to full).
6. Call `do_add_node` again from the same IP — it succeeds, violating the invariant.

The `get_available_add_node_capacity` helper (gated `#[cfg(test)]`) at [4](#0-3) 
can be used directly to assert the reset.

### Citations

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

**File:** rs/registry/canister/src/rate_limits.rs (L221-224)
```rust
#[cfg(test)]
pub fn get_available_add_node_capacity(ip_addr: String, now: SystemTime) -> u64 {
    with_add_node_ip_rate_limiter(|rate_limiter| rate_limiter.get_available_capacity(ip_addr, now))
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

**File:** rs/registry/canister/src/mutations/node_management/do_add_node.rs (L60-90)
```rust
        let reservation =
            self.try_reserve_capacity_for_node_operator_operation(now, caller_id, 1)?;

        // Validate keys and get the node id
        let (node_id, valid_pks) = valid_keys_from_payload(&payload)
            .map_err(|err| format!("{LOG_PREFIX}do_add_node: {err}"))?;

        println!("{LOG_PREFIX}do_add_node: The node id is {node_id:?}");

        // Get valid node_rewards_type if type is in request
        let node_reward_type = payload
            .node_reward_type
            .as_ref()
            .map(|t| {
                validate_str_as_node_reward_type(t).map_err(|e| {
                    format!("{LOG_PREFIX}do_add_node: Error parsing node type from payload: {e}")
                })
            })
            .transpose()?;

        // Clear out any nodes that already exist at this IP.
        // This will only succeed if the same NO was in control of the original nodes.
        //
        // (We use the http endpoint to be in line with what is used by the
        // release dashboard.)
        let http_endpoint = connection_endpoint_from_string(&payload.http_endpoint);

        // 2a. Check IP-based rate limiting (1 node addition per day per IP)
        let ip_addr = http_endpoint.ip_addr.clone();
        let ip_reservation = try_reserve_add_node_capacity(now, ip_addr.clone())
            .map_err(|e| format!("{LOG_PREFIX}do_add_node: {e}"))?;
```
