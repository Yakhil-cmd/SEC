### Title
Missing Same-Value Guard in `do_update_node_operator_config_directly` Causes Unnecessary Registry Version Increment and Rate-Limit Consumption - (File: `rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs`)

### Summary
The `do_update_node_operator_config_directly` function in the Registry canister, callable by any node provider as an unprivileged ingress sender, does not check whether the submitted `node_provider_id` equals the value already stored in the `NodeOperatorRecord`. As a result, a no-op call (same value) unconditionally increments the global registry version and consumes the caller's rate-limit capacity, producing unnecessary side effects with no actual state change.

### Finding Description
`do_update_node_operator_config_directly_` at lines 33–100 of `rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs` performs the following steps unconditionally after authorization:

1. Reserves rate-limit capacity (line 70).
2. Writes the (possibly identical) `node_provider_id` into the record (line 83).
3. Applies a `RegistryMutation::Update` (lines 86–93), which calls `maybe_apply_mutation_internal` → `apply_mutations` → `increment_version` in `rs/registry/canister/src/registry.rs` (lines 233–327), unconditionally bumping the global registry version.
4. Commits the rate-limit usage (line 95).

There is no guard of the form:
```rust
if node_provider_id.to_vec() == node_operator_record.node_provider_principal_id {
    return Err("SAME_VALUE".to_string());
}
```
before the mutation is applied. [1](#0-0) [2](#0-1) 

The canister entry point is open to any caller (not just governance): [3](#0-2) 

The rate-limit side-effect is confirmed by the existing test: [4](#0-3) 

### Impact Explanation
Every no-op call:

- **Increments the global registry version** — all replica nodes and registry clients across the IC network must fetch and process a new changelog entry that carries no actual change. This is unnecessary certified-state churn.
- **Consumes the node provider's rate-limit slot** — the rate-limit is a finite, time-windowed resource. Exhausting it with no-op calls blocks the node provider from making legitimate `node_provider_id` changes until the window resets, as confirmed by the failure test: [5](#0-4) 

### Likelihood Explanation
Any node provider who controls a `NodeOperatorRecord` can trigger this by sending an ingress update to `update_node_operator_config_directly` with their own current `node_provider_id`. No governance proposal, no privileged key, and no subnet-majority is required. The rate-limit bounds the frequency but does not prevent the issue.

### Recommendation
Add a same-value guard immediately before the mutation is applied:

```rust
if node_provider_id.to_vec() == node_operator_record.node_provider_principal_id {
    return Err(format!(
        "Node provider ID is already set to {node_provider_id}; no change needed."
    ));
}
```

This should be inserted after step 4 (the `node_provider_id == node_operator_id` check) and before `node_operator_record.node_provider_principal_id = node_provider_id.to_vec();`, so that neither the registry version nor the rate-limit slot is consumed for a no-op. [6](#0-5) 

### Proof of Concept

1. Node provider `NP_A` owns a `NodeOperatorRecord` where `node_provider_principal_id = NP_A`.
2. `NP_A` sends an ingress update to `update_node_operator_config_directly` with `node_provider_id = NP_A` (identical to the stored value).
3. Authorization passes (caller == stored NP).
4. Rate-limit slot is reserved and committed.
5. A `RegistryMutation::Update` is applied, incrementing the global registry version.
6. All subnet nodes fetch the new registry version and find no meaningful change.
7. Repeat up to the rate-limit ceiling — the node provider's capacity is exhausted, blocking legitimate future calls with `"Rate Limit Capacity exceeded."`. [7](#0-6)

### Citations

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs (L33-100)
```rust
    fn do_update_node_operator_config_directly_(
        &mut self,
        payload: UpdateNodeOperatorConfigDirectlyPayload,
        caller: PrincipalId,
        now: SystemTime,
    ) -> Result<(), String> {
        println!("{LOG_PREFIX}do_update_node_operator_config_directly: {payload:?}");

        // 1. Look up the record of the requested target NodeOperatorRecord.
        let node_operator_id = payload
            .node_operator_id
            .ok_or("No Node Operator specified in the payload".to_string())?;

        let node_operator_record_key = make_node_operator_record_key(node_operator_id).into_bytes();
        let node_operator_record_vec = &self
            .get(&node_operator_record_key, self.latest_version())
            .ok_or(format!(
                "Node Operator record with ID {node_operator_id} not found in the registry."
            ))?
            .value;

        let mut node_operator_record =
            NodeOperatorRecord::decode(node_operator_record_vec.as_slice())
                .map_err(|e| format!("{e:?}"))?;

        // 2. Make sure that the caller is authorized to make the requested changes to node_operator_record.
        if caller
            != PrincipalId::try_from(&node_operator_record.node_provider_principal_id).unwrap()
        {
            return Err(format!(
                "Caller {caller} not equal to the node_provider_princpal_id for this record."
            ));
        }

        // 3. Check Rate Limits
        let current_node_provider = caller;
        let reservation =
            self.try_reserve_capacity_for_node_provider_operation(now, current_node_provider, 1)?;

        // 4. Check that the Node Provider is not being set with the same ID as the Node Operator
        let node_provider_id = payload
            .node_provider_id
            .ok_or("No Node Provider specified in the payload".to_string())?;

        if node_provider_id == node_operator_id {
            return Err(format!(
                "The Node Operator ID cannot be the same as the Node Provider ID: {node_operator_id}"
            ));
        }

        node_operator_record.node_provider_principal_id = node_provider_id.to_vec();

        // 5. Set and execute the mutation
        let mutations = vec![RegistryMutation {
            mutation_type: registry_mutation::Type::Update as i32,
            key: node_operator_record_key,
            value: node_operator_record.encode_to_vec(),
        }];

        // Check invariants before applying mutations
        self.maybe_apply_mutation_internal(mutations);

        if let Err(e) = self.commit_used_capacity_for_node_provider_operation(now, reservation) {
            println!("{LOG_PREFIX}Error committing Rate Limit usage: {e}");
        }

        Ok(())
    }
```

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs (L172-210)
```rust
    #[test]
    fn test_update_node_operator_config_directly_affects_rate_limits() {
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

        // The original node provider should be able to change the node operator configuration.
        let caller = node_provider_id;

        let available = registry.get_available_node_provider_op_capacity(caller, now);

        registry
            .do_update_node_operator_config_directly_(request, caller, now)
            .unwrap();

        let next_available = registry.get_available_node_provider_op_capacity(caller, now);
        assert_eq!(available - 1, next_available);
    }
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

**File:** rs/registry/canister/src/registry.rs (L311-328)
```rust
    fn apply_mutations(&mut self, mutations: Vec<RegistryMutation>) {
        if mutations.is_empty() {
            // We should not increment the version if there is no
            // mutation, so that we keep the invariant that the
            // global version is the max of all versions in the store.
            return;
        }

        let mutations = RegistryAtomicMutateRequest {
            mutations,
            preconditions: vec![],
        };
        let mut mutations = chunkify_composite_mutation_if_too_large(mutations);
        mutations.timestamp_nanoseconds = now_nanoseconds();

        self.increment_version();
        self.apply_mutations_as_version(mutations, self.version);
    }
```

**File:** rs/registry/canister/canister/canister.rs (L809-823)
```rust
#[unsafe(export_name = "canister_update update_node_operator_config_directly")]
fn update_node_operator_config_directly() {
    // This method can be called by anyone
    println!(
        "{}call: update_node_operator_config_directly from: {}",
        LOG_PREFIX,
        dfn_core::api::caller()
    );
    over(
        candid_one,
        |payload: UpdateNodeOperatorConfigDirectlyPayload| {
            update_node_operator_config_directly_(payload)
        },
    );
}
```
