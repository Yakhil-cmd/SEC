The code is clear. Let me trace the exact asymmetry.

The asymmetry is confirmed in the code. Here is the analysis:

---

### Title
Provider Rate Limit Exhaustion via `do_update_node_operator_config_directly` Blocks All `add_node`/`remove_node` Operations — (`rs/registry/canister/src/rate_limits.rs`, `rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs`)

### Summary

`do_update_node_operator_config_directly` charges only the `NODE_PROVIDER_RATE_LIMITER`, while `do_add_node` and `do_remove_node_directly` charge **both** `NODE_OPERATOR_RATE_LIMITER` and `NODE_PROVIDER_RATE_LIMITER`. A node provider can exhaust the shared provider-level pool via the cheaper update path, permanently blocking all `add_node`/`remove_node` operations for every operator under that provider until the limiter refills.

### Finding Description

**Rate-limiting asymmetry:**

`do_update_node_operator_config_directly_` calls only `try_reserve_capacity_for_node_provider_operation`: [1](#0-0) 

`do_add_node_` calls `try_reserve_capacity_for_node_operator_operation`, which internally reserves from **both** the operator and provider limiters: [2](#0-1) [3](#0-2) 

`do_remove_node_directly_` has the same dual-limiter pattern: [4](#0-3) 

The provider spike cap is `NODE_PROVIDER_MAX_SPIKE = 20 × 7 = 140`: [5](#0-4) 

**No-op call is possible:** The only guard in `do_update_node_operator_config_directly_` is that `node_provider_id ≠ node_operator_id`. There is no check that the new `node_provider_id` differs from the current one, so the caller can submit the same provider ID repeatedly, making each call a state-preserving no-op that still consumes one unit of provider capacity: [6](#0-5) 

**Attack sequence:**
1. Attacker is the `node_provider_principal_id` for one or more operator records.
2. Attacker calls `do_update_node_operator_config_directly` 140 times (same `node_provider_id` as current, different from `node_operator_id`), each consuming 1 unit from `NODE_PROVIDER_RATE_LIMITER`.
3. Provider capacity reaches 0.
4. Any subsequent `add_node` or `remove_node` call by **any** operator under that provider fails at the provider reservation step inside `try_reserve_capacity_for_node_operator_operation`, even though the operator-level limiter still has full capacity.

### Impact Explanation

All `add_node` and `remove_node` operations for every node operator under the targeted provider are blocked. The operator-level rate limits are untouched, so the block is invisible from the operator's perspective — they have capacity, but the provider pool is dry. The limiter refills at 1 unit per `NODE_PROVIDER_CAPACITY_ADD_INTERVAL_SECONDS` (~72 minutes per unit), so full recovery from 0 takes ~7 days. [7](#0-6) 

### Likelihood Explanation

The attacker only needs to be the `node_provider_principal_id` for at least one operator record and send 140 ingress messages to the registry canister. No governance majority, no admin key, no threshold corruption is required. The call is cheap and the effect is immediate.

### Recommendation

`do_update_node_operator_config_directly` should consume from **both** rate limiters (i.e., call `try_reserve_capacity_for_node_operator_operation` instead of `try_reserve_capacity_for_node_provider_operation`), consistent with `add_node` and `remove_node`. Alternatively, add a guard rejecting calls where the submitted `node_provider_id` equals the current value, eliminating the no-op exhaustion vector.

### Proof of Concept

```
State-machine test sketch:
1. Register node_provider P and node_operator O (O.node_provider = P).
2. For i in 0..140:
       call do_update_node_operator_config_directly(node_operator_id=O, node_provider_id=P)
       // same provider ID → no-op mutation, but provider capacity -= 1 each time
3. Assert get_available_node_provider_op_capacity(P) == 0
4. Assert get_available_node_operator_op_capacity(O) == 140  // untouched
5. Call do_add_node(caller=O, ...)
6. Assert error == "Rate Limit Capacity exceeded. Please wait and try again later."
```

This directly mirrors the existing test pattern in `test_do_add_node_fails_when_rate_limits_exceeded` and `test_update_node_operator_config_directly_fails_when_rate_limits_exceeded`, confirming the scenario is locally reproducible without any privileged infrastructure access. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs (L67-70)
```rust
        // 3. Check Rate Limits
        let current_node_provider = caller;
        let reservation =
            self.try_reserve_capacity_for_node_provider_operation(now, current_node_provider, 1)?;
```

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs (L77-83)
```rust
        if node_provider_id == node_operator_id {
            return Err(format!(
                "The Node Operator ID cannot be the same as the Node Provider ID: {node_operator_id}"
            ));
        }

        node_operator_record.node_provider_principal_id = node_provider_id.to_vec();
```

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs (L212-258)
```rust
    #[test]
    fn test_update_node_operator_config_directly_fails_when_rate_limits_exceeded() {
        let mut registry = invariant_compliant_registry(0);

        let now = now_system_time();

        let node_operator_id = PrincipalId::new_user_test_id(1_000);
        let node_provider_id = PrincipalId::new_user_test_id(10_000);

        // Make a proposal to upgrade all unassigned nodes to a new version
        let payload = AddNodeOperatorPayload {
            node_operator_principal_id: Some(node_operator_id),
            node_provider_principal_id: Some(node_provider_id),
            node_allowance: 1,
            dc_id: "DC1".to_string(),
            rewardable_nodes: btreemap! { "type1.1".to_string() => 1 },
            ipv6: Some("bar".to_string()),
            max_rewardable_nodes: Some(btreemap! { "type1.2".to_string() => 1 }),
        };

        registry.do_add_node_operator(payload);

        let request = UpdateNodeOperatorConfigDirectlyPayload {
            node_operator_id: Some(node_operator_id),
            node_provider_id: Some(node_provider_id),
        };

        // Max out node provider operations
        let available = registry.get_available_node_provider_op_capacity(node_provider_id, now);
        let reservation = registry
            .try_reserve_capacity_for_node_provider_operation(now, node_provider_id, available)
            .unwrap();
        registry
            .commit_used_capacity_for_node_provider_operation(now, reservation)
            .unwrap();

        // The original node provider should be able to change the node operator configuration.
        let caller = node_provider_id;
        let error = registry
            .do_update_node_operator_config_directly_(request, caller, now)
            .unwrap_err();

        assert_eq!(
            error,
            "Rate Limit Capacity exceeded. Please wait and try again later."
        );
    }
```

**File:** rs/registry/canister/src/mutations/node_management/do_add_node.rs (L60-61)
```rust
        let reservation =
            self.try_reserve_capacity_for_node_operator_operation(now, caller_id, 1)?;
```

**File:** rs/registry/canister/src/mutations/node_management/do_add_node.rs (L1213-1259)
```rust
    #[test]
    fn test_do_add_node_fails_when_rate_limits_exceeded() {
        let mut registry = invariant_compliant_registry(0);

        let now = now_system_time();

        let node_operator_id = PrincipalId::new_user_test_id(1_000);
        let node_provider_id = PrincipalId::new_user_test_id(10_000);

        // Add node operator record first
        let node_operator_record = NodeOperatorRecord {
            node_operator_principal_id: node_operator_id.to_vec(),
            node_provider_principal_id: node_provider_id.to_vec(),
            node_allowance: 1,
            dc_id: "DC1".to_string(),
            rewardable_nodes: btreemap! { "type1.1".to_string() => 1 },
            ipv6: Some("bar".to_string()),
            max_rewardable_nodes: btreemap! { "type1.2".to_string() => 1 },
        };

        registry.maybe_apply_mutation_internal(vec![insert(
            make_node_operator_record_key(node_operator_id),
            node_operator_record.encode_to_vec(),
        )]);

        let (payload, _) = prepare_add_node_payload(1, NodeRewardType::Type1);
        // Exhaust the rate limit capacity
        let available_operator =
            registry.get_available_node_operator_op_capacity(node_operator_id, now);
        let available_provider =
            registry.get_available_node_provider_op_capacity(node_provider_id, now);
        let available = available_operator.min(available_provider);
        let reservation = registry
            .try_reserve_capacity_for_node_operator_operation(now, node_operator_id, available)
            .unwrap();
        registry
            .commit_used_capacity_for_node_operator_operation(now, reservation)
            .unwrap();

        let error = registry
            .do_add_node_(payload, node_operator_id, now)
            .unwrap_err();

        assert_eq!(
            error,
            "Rate Limit Capacity exceeded. Please wait and try again later."
        );
```

**File:** rs/registry/canister/src/rate_limits.rs (L18-21)
```rust
const NODE_PROVIDER_MAX_AVG_OPERATIONS_PER_DAY: u64 = 20;
const NODE_PROVIDER_MAX_SPIKE: u64 = NODE_PROVIDER_MAX_AVG_OPERATIONS_PER_DAY * 7;
pub const NODE_PROVIDER_CAPACITY_ADD_INTERVAL_SECONDS: u64 =
    ONE_DAY_SECONDS / NODE_PROVIDER_MAX_AVG_OPERATIONS_PER_DAY;
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

**File:** rs/registry/canister/src/mutations/node_management/do_remove_node_directly.rs (L53-54)
```rust
        let reservation =
            self.try_reserve_capacity_for_node_operator_operation(now, node_operator_id, 1)?;
```
