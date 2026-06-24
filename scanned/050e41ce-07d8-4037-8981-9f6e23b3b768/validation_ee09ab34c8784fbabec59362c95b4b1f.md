The rate limiting code is clear. Let me confirm the exact constants and the starvation path.

### Title
Provider-Level Rate Limit Starvation: One Node Operator Can Exhaust Shared Provider Bucket, Blocking All Sibling Operators — (`rs/registry/canister/src/rate_limits.rs`)

---

### Summary

The provider-level leaky bucket (`NODE_PROVIDER_MAX_SPIKE = 140`) is sized identically to the per-operator bucket (`NODE_OPERATOR_MAX_SPIKE = 140`). Because `try_reserve_capacity_for_node_operator_operation` debits **both** the caller's operator bucket and the shared provider bucket on every operation, a single operator O1 can fully drain the provider bucket in 140 calls, leaving zero capacity for every sibling operator O2 under the same provider for at least one refill interval (72 minutes), and indefinitely if O1 keeps re-exhausting it.

---

### Finding Description

`try_reserve_capacity_for_node_operator_operation` in `rs/registry/canister/src/rate_limits.rs` performs two sequential reservations:

1. One unit from the **per-operator** bucket keyed by `node_operator_key(node_operator_id)` — capacity `NODE_OPERATOR_MAX_SPIKE = 140`.
2. One unit from the **per-provider** bucket keyed by `node_provider_key(node_provider_id)` — capacity `NODE_PROVIDER_MAX_SPIKE = 140`. [1](#0-0) 

Both buckets are configured with the same `max_capacity = 140`: [2](#0-1) 

Because the provider bucket is shared across all operators under the same provider, and its capacity equals one operator's full spike budget, O1 can drain it entirely within their own operator budget. After 140 committed operations by O1, the provider bucket is at zero. Any subsequent call by O2 to `try_reserve_capacity_for_node_operator_operation` fails at step 2 with `RateLimiterError::CapacityExceeded`, even though O2's own operator bucket is untouched.

The attack requires only successful calls. `do_update_node_ipv4_config_directly_` with `ipv4_config: None` passes all validation (the uniqueness check is skipped when the config is absent) and commits the reservation on every call, so O1 needs only a single owned node to issue 140 no-op IPv4 clears: [3](#0-2) 

The refill interval is `ONE_DAY_SECONDS / 20 = 4320 seconds` (72 minutes) per unit. O1 can re-exhaust the one unit that refills every 72 minutes, maintaining the DoS indefinitely at a cost of one ingress call per 72 minutes.

The existing `test_combined_rate_limiting` test verifies proportional depletion but does **not** assert that full exhaustion by one operator blocks a sibling: [4](#0-3) 

---

### Impact Explanation

O2 cannot call `add_node`, `remove_node_directly`, `update_node_domain_directly`, or `update_node_ipv4_config_directly` for as long as O1 keeps the provider bucket exhausted. All four operations route through `try_reserve_capacity_for_node_operator_operation` and fail at the provider-bucket step. The minimum lockout per exhaustion cycle is 72 minutes; with continuous re-exhaustion it is indefinite.

---

### Likelihood Explanation

The attacker must be a legitimate node operator registered under the same provider as the victim. This is a realistic relationship in the IC node operator ecosystem (a single node provider can have multiple operators). The attack requires no privileged access, no governance majority, and no key material — only 140 ingress calls to the registry canister, which is a public endpoint. The cost is negligible.

---

### Recommendation

Size the provider-level bucket to accommodate all operators under it fairly. Options:

1. **Increase `NODE_PROVIDER_MAX_SPIKE`** to a multiple of the operator spike (e.g., `N × 140` where N is the expected maximum number of operators per provider), so no single operator can exhaust the full provider budget.
2. **Add a per-operator cap on provider-bucket consumption** — after reserving from the operator bucket, cap the provider-bucket debit at `provider_max / max_operators_per_provider`.
3. **Add a cross-operator starvation test** to `rs/registry/canister/src/rate_limits.rs` that sets up two operators under one provider, exhausts the provider bucket via O1, and asserts O2's calls return a rate-limit error — making the design invariant explicit and regression-tested.

---

### Proof of Concept

```
State: O1 and O2 both registered under provider P.
       Provider bucket: 140/140. O1 operator bucket: 140/140. O2 operator bucket: 140/140.

Step 1: O1 calls do_update_node_ipv4_config_directly(node_id=N1, ipv4_config=None) × 140
        → each call: operator[O1] -= 1, provider[P] -= 1
        → after 140 calls: operator[O1] = 0, provider[P] = 0

Step 2: O2 calls do_update_node_ipv4_config_directly(node_id=N2, ipv4_config=None)
        → try_reserve: operator[O2] -= 1 (succeeds, 139 left)
        → try_reserve: provider[P] -= 1 (FAILS, 0 capacity)
        → operator reservation is dropped (not committed)
        → returns Err("Rate Limit Capacity exceeded. Please wait and try again later.")

Step 3: O1 waits 72 minutes, provider[P] refills to 1.
        O1 calls once more → provider[P] = 0 again.
        O2 remains blocked.
```

### Citations

**File:** rs/registry/canister/src/rate_limits.rs (L18-27)
```rust
const NODE_PROVIDER_MAX_AVG_OPERATIONS_PER_DAY: u64 = 20;
const NODE_PROVIDER_MAX_SPIKE: u64 = NODE_PROVIDER_MAX_AVG_OPERATIONS_PER_DAY * 7;
pub const NODE_PROVIDER_CAPACITY_ADD_INTERVAL_SECONDS: u64 =
    ONE_DAY_SECONDS / NODE_PROVIDER_MAX_AVG_OPERATIONS_PER_DAY;

// Node Operator rate limiting constants
const NODE_OPERATOR_MAX_AVG_OPERATIONS_PER_DAY: u64 = 20;
const NODE_OPERATOR_MAX_SPIKE: u64 = NODE_OPERATOR_MAX_AVG_OPERATIONS_PER_DAY * 7;
pub const NODE_OPERATOR_CAPACITY_ADD_INTERVAL_SECONDS: u64 =
    ONE_DAY_SECONDS / NODE_OPERATOR_MAX_AVG_OPERATIONS_PER_DAY;
```

**File:** rs/registry/canister/src/rate_limits.rs (L114-122)
```rust
        // First reserve from node operator rate limiter (primary)
        let operator_reservation = with_node_operator_rate_limiter(|rate_limiter| {
            rate_limiter.try_reserve(now, node_operator_key(node_operator_id), requested_capacity)
        })?;

        // Then reserve from node provider rate limiter (secondary)
        let provider_reservation = with_node_provider_rate_limiter(|rate_limiter| {
            rate_limiter.try_reserve(now, node_provider_key(node_provider_id), requested_capacity)
        })?;
```

**File:** rs/registry/canister/src/rate_limits.rs (L233-322)
```rust
    #[test]
    fn test_combined_rate_limiting() {
        let now = SystemTime::now();
        let shared_node_provider = PrincipalId::new_user_test_id(1000);
        let node_operator_1 = PrincipalId::new_user_test_id(1);
        let node_operator_2 = PrincipalId::new_user_test_id(2);
        let mut registry = invariant_compliant_registry(0);

        // Add first node operator that shares the node provider
        let payload_1 = AddNodeOperatorPayload {
            node_operator_principal_id: Some(node_operator_1),
            node_provider_principal_id: Some(shared_node_provider),
            node_allowance: 10,
            dc_id: "test_dc_1".to_string(),
            rewardable_nodes: btreemap! { "type1".to_string() => 1 },
            ipv6: None,
            max_rewardable_nodes: Some(btreemap! { "type1".to_string() => 1 }),
        };
        registry.do_add_node_operator(payload_1);

        // Add second node operator that shares the same node provider
        let payload_2 = AddNodeOperatorPayload {
            node_operator_principal_id: Some(node_operator_2),
            node_provider_principal_id: Some(shared_node_provider),
            node_allowance: 10,
            dc_id: "test_dc_2".to_string(),
            rewardable_nodes: btreemap! { "type1".to_string() => 1 },
            ipv6: None,
            max_rewardable_nodes: Some(btreemap! { "type1".to_string() => 1 }),
        };
        registry.do_add_node_operator(payload_2);

        // Get initial capacities
        let initial_operator_1_capacity =
            registry.get_available_node_operator_op_capacity(node_operator_1, now);
        let initial_operator_2_capacity =
            registry.get_available_node_operator_op_capacity(node_operator_2, now);
        let initial_provider_capacity =
            registry.get_available_node_provider_op_capacity(shared_node_provider, now);

        // Reserve capacity for first node operator
        let reservation_1 = registry
            .try_reserve_capacity_for_node_operator_operation(now, node_operator_1, 5)
            .unwrap();

        // Check that both operator and provider capacities decreased
        let after_operator_1_capacity =
            registry.get_available_node_operator_op_capacity(node_operator_1, now);
        let after_provider_capacity =
            registry.get_available_node_provider_op_capacity(shared_node_provider, now);
        assert_eq!(initial_operator_1_capacity - 5, after_operator_1_capacity);
        assert_eq!(initial_provider_capacity - 5, after_provider_capacity);

        // Reserve capacity for second node operator
        let reservation_2 = registry
            .try_reserve_capacity_for_node_operator_operation(now, node_operator_2, 3)
            .unwrap();

        // Check that second operator capacity decreased, but provider capacity decreased further
        let after_operator_2_capacity =
            registry.get_available_node_operator_op_capacity(node_operator_2, now);
        let final_provider_capacity =
            registry.get_available_node_provider_op_capacity(shared_node_provider, now);
        assert_eq!(initial_operator_2_capacity - 3, after_operator_2_capacity);
        assert_eq!(initial_provider_capacity - 5 - 3, final_provider_capacity);

        // Drop first reservation - should restore operator 1 capacity and provider capacity
        drop(reservation_1);

        let restored_operator_1_capacity =
            registry.get_available_node_operator_op_capacity(node_operator_1, now);
        let restored_provider_capacity =
            registry.get_available_node_provider_op_capacity(shared_node_provider, now);
        assert_eq!(initial_operator_1_capacity, restored_operator_1_capacity);
        assert_eq!(initial_provider_capacity - 3, restored_provider_capacity); // Only operator 2's reservation remains

        // Drop second reservation - should restore everything
        drop(reservation_2);

        let final_operator_1_capacity =
            registry.get_available_node_operator_op_capacity(node_operator_1, now);
        let final_operator_2_capacity =
            registry.get_available_node_operator_op_capacity(node_operator_2, now);
        let final_provider_capacity =
            registry.get_available_node_provider_op_capacity(shared_node_provider, now);

        assert_eq!(initial_operator_1_capacity, final_operator_1_capacity);
        assert_eq!(initial_operator_2_capacity, final_operator_2_capacity);
        assert_eq!(initial_provider_capacity, final_provider_capacity);
    }
```

**File:** rs/registry/canister/src/mutations/node_management/do_update_node_ipv4_config_directly.rs (L48-76)
```rust
        let reservation =
            self.try_reserve_capacity_for_node_operator_operation(now, node_operator_id, 1)?;

        // Ensure payload is valid
        self.validate_update_node_ipv4_config_directly_payload(&payload);

        // Get existing node record and apply the changes
        let mut node_record = self.get_node_or_panic(node_id);

        node_record.public_ipv4_config =
            payload.ipv4_config.map(|ipv4_config| IPv4InterfaceConfig {
                ip_addr: ipv4_config.ip_addr().to_string(),
                gateway_ip_addr: vec![ipv4_config.gateway_ip_addr().to_string()],
                prefix_length: ipv4_config.prefix_length(),
            });

        // Create the mutation
        let update_node_record = update(
            make_node_record_key(node_id).as_bytes(),
            node_record.encode_to_vec(),
        );
        let mutations = vec![update_node_record];

        // Check invariants before applying the mutation
        self.maybe_apply_mutation_internal(mutations);

        if let Err(e) = self.commit_used_capacity_for_node_operator_operation(now, reservation) {
            println!("{LOG_PREFIX}Error committing Rate Limit usage: {e}");
        }
```
