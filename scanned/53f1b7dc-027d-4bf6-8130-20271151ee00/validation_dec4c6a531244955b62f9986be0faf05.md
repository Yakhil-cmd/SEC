### Title
Provider-Level Rate Limit Starvation: One Node Operator Can Lock Out All Sibling Operators Under the Same Provider — (`rs/registry/canister/src/rate_limits.rs`)

### Summary

The provider-level leaky-bucket is sized identically to a single operator-level bucket (`NODE_PROVIDER_MAX_SPIKE = 140`), and it is shared across every node operator registered under the same node provider. Because a single operator's individual bucket is also 140, one operator can fully drain the shared provider bucket in a single burst, leaving zero capacity for every sibling operator under that provider for up to 7 days.

### Finding Description

`try_reserve_capacity_for_node_operator_operation` enforces two sequential rate-limit checks: first the per-operator bucket, then the per-provider bucket. [1](#0-0) 

Both buckets are configured with the same `max_capacity`: [2](#0-1) 

`NODE_PROVIDER_MAX_SPIKE = 20 × 7 = 140`. The provider bucket is keyed only by `node_provider_id`, so every operator under provider P draws from the same 140-token pool. [3](#0-2) 

The attack path through `do_update_node_ipv4_config_directly_`:

1. Rate-limit reservation is taken **before** payload validation and mutation.
2. The mutation is committed, then `commit_used_capacity_for_node_operator_operation` is called — permanently consuming one token from both the operator and provider buckets per successful call. [4](#0-3) 

O1 alternates between `ipv4_config: Some(...)` and `ipv4_config: None` on a node they own. Each round-trip is a valid call (no duplicate-IP conflict, no invariant violation). After 140 such calls O1's operator bucket and the shared provider bucket are both at 0.

When O2 then calls any rate-limited node-management operation, the operator reservation succeeds (O2's own bucket is untouched), but the provider reservation immediately fails: [5](#0-4) 

O2 receives `"Rate Limit Capacity exceeded. Please wait and try again later."` for every subsequent call until the provider bucket refills at 1 token per `ONE_DAY_SECONDS / 20 ≈ 4320 s`, taking up to 7 days to fully recover.

The existing `test_combined_rate_limiting` test verifies that shared-provider accounting is correct, but it does **not** test the starvation scenario where O1 exhausts the full provider budget. [6](#0-5) 

### Impact Explanation

O2 is completely locked out of all node-management operations gated by `try_reserve_capacity_for_node_operator_operation` — `add_node`, `remove_node`, `update_node_domain`, and `update_node_ipv4_config_directly` — for up to 7 days. This is an operational denial-of-service against a legitimate, independent node operator caused by a peer operator under the same provider.

### Likelihood Explanation

The preconditions are reachable in production: multiple node operators can and do share a single node provider principal. O1 needs only one node they own and 140 ingress calls (alternating set/clear of IPv4 config). No governance majority, no key compromise, and no subnet-level attack is required. The call sequence is entirely through normal ingress.

### Recommendation

- **Size the provider bucket proportionally**: `NODE_PROVIDER_MAX_SPIKE` should scale with the number of operators registered under the provider, or be set to a value that cannot be fully consumed by a single operator's budget (e.g., `N_operators × NODE_OPERATOR_MAX_SPIKE`).
- **Alternatively, enforce a per-operator sub-limit within the provider bucket**: cap the amount any single operator can draw from the provider pool per window, independent of their own operator bucket.
- **Add a starvation regression test**: extend `test_combined_rate_limiting` to assert that after O1 exhausts their full operator budget, O2's calls still succeed (i.e., the provider bucket must have remaining capacity for O2).

### Proof of Concept

```
State: provider P, operators O1 and O2 both registered under P.
       O1 owns node N.

for i in 1..=70:
    O1 → update_node_ipv4_config_directly(node=N, ipv4_config=Some(IP_A))
         # consumes 1 from O1-operator bucket, 1 from P-provider bucket
    O1 → update_node_ipv4_config_directly(node=N, ipv4_config=None)
         # consumes 1 from O1-operator bucket, 1 from P-provider bucket

# After 140 calls: O1-operator bucket = 0, P-provider bucket = 0

O2 → update_node_ipv4_config_directly(node=M, ipv4_config=Some(IP_B))
     # O2-operator reservation: OK (O2's bucket = 140)
     # P-provider reservation: FAIL → "Rate Limit Capacity exceeded"
     # O2 is blocked for up to 7 days
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

**File:** rs/registry/canister/src/rate_limits.rs (L72-78)
```rust
fn node_provider_key(node_provider: PrincipalId) -> String {
    format!("node_provider_{node_provider}")
}

fn node_operator_key(node_operator: PrincipalId) -> String {
    format!("node_operator_{node_operator}")
}
```

**File:** rs/registry/canister/src/rate_limits.rs (L114-127)
```rust
        // First reserve from node operator rate limiter (primary)
        let operator_reservation = with_node_operator_rate_limiter(|rate_limiter| {
            rate_limiter.try_reserve(now, node_operator_key(node_operator_id), requested_capacity)
        })?;

        // Then reserve from node provider rate limiter (secondary)
        let provider_reservation = with_node_provider_rate_limiter(|rate_limiter| {
            rate_limiter.try_reserve(now, node_provider_key(node_provider_id), requested_capacity)
        })?;

        Ok(RateLimitReservation {
            operator_reservation,
            provider_reservation,
        })
```

**File:** rs/registry/canister/src/rate_limits.rs (L234-322)
```rust
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
