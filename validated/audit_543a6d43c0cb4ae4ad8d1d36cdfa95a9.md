### Title
Cross-Operator Rate-Limit Drain: operator2 exhausts operator1's node-operation bucket via `remove_node_directly` — (`rs/registry/canister/src/mutations/node_management/do_remove_node_directly.rs`)

---

### Summary

`do_remove_node_directly_` charges the rate-limit cost against the **node's registered operator** (`node_operator_id`), not against the **caller**. Because the same function also permits a caller from a different operator record to remove nodes when DC and node-provider match, a sibling operator (operator2, same DC + same provider) can remove all of operator1's nodes while every removal is billed to operator1's rate-limit bucket. Once the bucket is drained, operator1 is blocked from `add_node` and `remove_node_directly` for up to 7 days.

---

### Finding Description

**Step 1 — Rate limit is reserved against the node's owner, not the caller.**

In `do_remove_node_directly_`:

```
node_operator_id = get_node_operator_id_for_node(self, payload.node_id)?;   // operator1
reservation = try_reserve_capacity_for_node_operator_operation(now, node_operator_id, 1)?;
// ↑ charges operator1's bucket regardless of who called
``` [1](#0-0) 

The authorization check (`caller_id` vs `node_operator_id`) happens only afterwards, inside `make_remove_or_replace_node_mutations`. [2](#0-1) 

**Step 2 — The authorization check explicitly allows a sibling operator.**

If `caller_id != node_operator_id`, the code falls back to comparing DC and node-provider. If both match, the removal succeeds. [3](#0-2) 

This is confirmed by the passing test `should_succeed_remove_node_compare_dc_and_node_provider`, which shows operator2 (same DC + same provider) successfully removing operator1's node. [4](#0-3) 

**Step 3 — Rate-limit parameters.**

The per-operator bucket has a max spike of `NODE_OPERATOR_MAX_SPIKE = 140` operations, refilling at 20 ops/day (7-day full-refill window). [5](#0-4) 

`try_reserve_capacity_for_node_operator_operation` charges both the operator bucket (keyed on `node_operator_id`) and the provider bucket (keyed on the provider derived from `node_operator_id`). [6](#0-5) 

**Step 4 — `add_node` uses the same per-operator bucket.**

`do_add_node_` calls `try_reserve_capacity_for_node_operator_operation(now, caller_id, 1)`, so an exhausted operator bucket also blocks node additions. [7](#0-6) 

---

### Impact Explanation

operator2 removes all of operator1's nodes (up to 140 in a burst). Each removal is billed to operator1's rate-limit bucket. Once the bucket is at zero, operator1 receives `"Rate Limit Capacity exceeded. Please wait and try again later."` on every `add_node` and `remove_node_directly` call for up to 7 days. This prevents operator1 from re-registering replacement hardware during the lockout window.

---

### Likelihood Explanation

The precondition — operator2 sharing DC and node-provider with operator1 — is a normal production configuration (a node provider running multiple operator records in the same data center). The `remove_node_directly` endpoint is callable by any authenticated principal; no governance proposal is required. The attack requires only that operator2 submit N ingress messages to the registry canister, one per node owned by operator1.

---

### Recommendation

Charge the rate-limit cost against the **caller** (`caller_id`), not the node's registered operator. In `do_remove_node_directly_`, replace:

```rust
let node_operator_id = get_node_operator_id_for_node(self, payload.node_id)?;
let reservation =
    self.try_reserve_capacity_for_node_operator_operation(now, node_operator_id, 1)?;
```

with:

```rust
let node_operator_id = get_node_operator_id_for_node(self, payload.node_id)?;
let reservation =
    self.try_reserve_capacity_for_node_operator_operation(now, caller_id, 1)?;
```

This ensures that operator2's actions consume operator2's budget, leaving operator1's budget unaffected.

---

### Proof of Concept

```
Setup:
  operator1: node_provider=P, dc=DC1, owns nodes [N1..N140]
  operator2: node_provider=P, dc=DC1, owns no nodes

Attack (operator2 sends 140 ingress messages):
  for node_id in [N1..N140]:
      registry.remove_node_directly(RemoveNodeDirectlyPayload { node_id })

Result:
  - operator1's 140 nodes are removed from the registry
  - operator1's rate-limit bucket is at 0
  - operator1 calls add_node → "Rate Limit Capacity exceeded. Please wait and try again later."
  - operator1 is locked out for up to 7 days
```

The existing unit test `should_succeed_remove_node_compare_dc_and_node_provider` already proves step 1 (operator2 can remove operator1's node). The existing test `test_do_remove_node_directly_fails_when_rate_limits_exceeded` proves step 2 (exhausted bucket blocks further operations). Combining both confirms the full attack path. [4](#0-3) [8](#0-7)

### Citations

**File:** rs/registry/canister/src/mutations/node_management/do_remove_node_directly.rs (L51-54)
```rust
        // Get the node operator ID for this node
        let node_operator_id = get_node_operator_id_for_node(self, payload.node_id)?;
        let reservation =
            self.try_reserve_capacity_for_node_operator_operation(now, node_operator_id, 1)?;
```

**File:** rs/registry/canister/src/mutations/node_management/do_remove_node_directly.rs (L56-58)
```rust
        let mutations = self.make_remove_or_replace_node_mutations(payload, caller_id, None);
        // Check invariants and apply mutations
        self.maybe_apply_mutation_internal(mutations);
```

**File:** rs/registry/canister/src/mutations/node_management/do_remove_node_directly.rs (L89-119)
```rust
        if caller_id != node_operator_id {
            let node_operator_caller = get_node_operator_record(self, caller_id)
                .map_err(|e| {
                    format!("{LOG_PREFIX}do_remove_node_directly: Aborting node removal: {e}")
                })
                .unwrap();
            let dc_caller = node_operator_caller.dc_id;
            let dc_orig_node_operator = get_node_operator_record(self, node_operator_id)
                .map_err(|e| {
                    format!("{LOG_PREFIX}do_remove_node_directly: Aborting node removal: {e}")
                })
                .unwrap()
                .dc_id;
            assert_eq!(
                dc_caller, dc_orig_node_operator,
                "The DC {dc_caller} of the caller {caller_id}, does not match the DC of the node {dc_orig_node_operator}."
            );
            let node_provider_caller = get_node_provider_id_for_operator_id(self, caller_id)
                .map_err(|e| {
                    format!("{LOG_PREFIX}do_remove_node_directly: Aborting node removal: {e}")
                });
            let node_provider_of_the_node =
                get_node_provider_id_for_operator_id(self, node_operator_id).map_err(|e| {
                    format!("{LOG_PREFIX}do_remove_node_directly: Aborting node removal: {e}")
                });
            assert_eq!(
                node_provider_caller, node_provider_of_the_node,
                "The node provider {:?} of the caller {}, does not match the node provider {:?} of the node {}.",
                node_provider_caller, caller_id, node_provider_of_the_node, payload.node_id
            );
        }
```

**File:** rs/registry/canister/src/mutations/node_management/do_remove_node_directly.rs (L482-554)
```rust
    #[test]
    fn should_succeed_remove_node_compare_dc_and_node_provider() {
        let mut registry = invariant_compliant_registry(0);
        // Add node operator1 and operator2 records, both under the same provider
        let operator1_id = PrincipalId::new_user_test_id(2000);
        let operator2_id = PrincipalId::new_user_test_id(2001);
        let operator_record_1 = NodeOperatorRecord {
            node_operator_principal_id: operator1_id.to_vec(),
            node_provider_principal_id: PrincipalId::new_user_test_id(3000).to_vec(),
            dc_id: "dc1".to_string(),
            node_allowance: 1,
            rewardable_nodes: btreemap! { "type0".to_string() => 0, "type1".to_string() => 28 },
            ..Default::default()
        };
        let operator_record_2 = NodeOperatorRecord {
            node_operator_principal_id: operator2_id.to_vec(),
            node_provider_principal_id: PrincipalId::new_user_test_id(3000).to_vec(),
            dc_id: "dc1".to_string(),
            node_allowance: 1,
            rewardable_nodes: btreemap! { "type1.1".to_string() => 28 },
            ..Default::default()
        };
        registry.maybe_apply_mutation_internal(vec![
            insert(
                make_node_operator_record_key(operator1_id),
                operator_record_1.encode_to_vec(),
            ),
            insert(
                make_node_operator_record_key(operator2_id),
                operator_record_2.encode_to_vec(),
            ),
        ]);
        // Add node owned by operator1 to registry
        let (mutate_request, node_ids_and_dkg_pks) =
            prepare_registry_with_nodes_and_node_operator_id(
                1, /* mutation id */
                1, /* node count */
                operator1_id,
            );
        registry.maybe_apply_mutation_internal(mutate_request.mutations);
        let node_id = node_ids_and_dkg_pks
            .keys()
            .next()
            .expect("should contain at least one node ID")
            .to_owned();
        let original_operator_record_1 =
            get_node_operator_record(&registry, operator1_id).expect("failed to get node operator");
        let original_operator_record_2 =
            get_node_operator_record(&registry, operator2_id).expect("failed to get node operator");

        let payload = RemoveNodeDirectlyPayload { node_id };

        // Should succeed because both operator1 and operator2 are under the same provider
        let _ = registry.do_remove_node_directly_(payload, operator2_id, now_system_time());

        let expected_operator_record_1 = NodeOperatorRecord {
            node_allowance: original_operator_record_1.node_allowance + 1,
            ..original_operator_record_1
        };
        let expected_operator_record_2 = NodeOperatorRecord {
            node_allowance: original_operator_record_2.node_allowance,
            ..original_operator_record_2
        };
        let actual_operator_record_1 =
            get_node_operator_record(&registry, operator1_id).expect("failed to get node operator");
        println!("node_operator_1_record: {actual_operator_record_1:#?}");
        let actual_operator_record_2 =
            get_node_operator_record(&registry, operator2_id).expect("failed to get node operator");
        println!("node_operator_2_record: {actual_operator_record_2:#?}");

        assert_eq!(actual_operator_record_1, expected_operator_record_1);
        assert_eq!(actual_operator_record_2, expected_operator_record_2);
    }
```

**File:** rs/registry/canister/src/mutations/node_management/do_remove_node_directly.rs (L692-721)
```rust
    fn test_do_remove_node_directly_fails_when_rate_limits_exceeded() {
        let (mut registry, node_ids, node_operator_id, node_provider_id) =
            setup_registry_for_test();
        let node_id = node_ids[0];

        let now = now_system_time();

        let payload = RemoveNodeDirectlyPayload { node_id };

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
            .do_remove_node_directly_(payload, node_operator_id, now)
            .unwrap_err();
        assert_eq!(
            error,
            "Rate Limit Capacity exceeded. Please wait and try again later."
        );
    }
```

**File:** rs/registry/canister/src/rate_limits.rs (L23-27)
```rust
// Node Operator rate limiting constants
const NODE_OPERATOR_MAX_AVG_OPERATIONS_PER_DAY: u64 = 20;
const NODE_OPERATOR_MAX_SPIKE: u64 = NODE_OPERATOR_MAX_AVG_OPERATIONS_PER_DAY * 7;
pub const NODE_OPERATOR_CAPACITY_ADD_INTERVAL_SECONDS: u64 =
    ONE_DAY_SECONDS / NODE_OPERATOR_MAX_AVG_OPERATIONS_PER_DAY;
```

**File:** rs/registry/canister/src/rate_limits.rs (L104-128)
```rust
    pub fn try_reserve_capacity_for_node_operator_operation(
        &self,
        now: SystemTime,
        node_operator_id: PrincipalId,
        requested_capacity: u64,
    ) -> Result<RateLimitReservation, RateLimiterError> {
        // Find the associated node provider ID for this node operator
        let node_provider_id = get_node_provider_id_for_operator_id(self, node_operator_id)
            .map_err(RateLimiterError::InvalidArguments)?;

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
    }
```

**File:** rs/registry/canister/src/mutations/node_management/do_add_node.rs (L60-61)
```rust
        let reservation =
            self.try_reserve_capacity_for_node_operator_operation(now, caller_id, 1)?;
```
