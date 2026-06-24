### Title
`rewardable_nodes` Cannot Be Cleared to Empty Map via Governance Proposal — Silent No-Op in `do_update_node_operator_config` - (File: `rs/registry/canister/src/mutations/do_update_node_operator_config.rs`)

---

### Summary

`do_update_node_operator_config` in the IC Registry canister silently skips updating `rewardable_nodes` when the payload contains an empty map, mirroring the exact bug class reported in `PairInfos.sol`. A governance proposal intended to clear a node operator's `rewardable_nodes` (and thus stop reward payments) will execute without error but leave the registry state unchanged, causing the node operator to continue receiving rewards indefinitely.

---

### Finding Description

In `rs/registry/canister/src/mutations/do_update_node_operator_config.rs`, the `do_update_node_operator_config` function applies the following guard before updating `rewardable_nodes`:

```rust
if !payload.rewardable_nodes.is_empty() {
    node_operator_record.rewardable_nodes = payload.rewardable_nodes;
}
``` [1](#0-0) 

The `rewardable_nodes` field in `UpdateNodeOperatorConfigPayload` is typed as `BTreeMap<String, u32>` (not `Option<BTreeMap<...>>`), so there is no way to distinguish "caller did not provide this field" from "caller explicitly wants to set it to empty." [2](#0-1) 

When a governance proposal submits an empty `rewardable_nodes` map to clear all reward entries for a node operator, the guard silently skips the assignment. The mutation is still written to the registry (the outer `RegistryMutation` is always applied), but the `rewardable_nodes` field in the encoded `NodeOperatorRecord` retains its old value. The proposal returns success with no error.

The same pattern exists for `max_rewardable_nodes`:

```rust
if let Some(max_rewardable_nodes) = payload.max_rewardable_nodes {
    if !max_rewardable_nodes.is_empty() {
        node_operator_record.max_rewardable_nodes = max_rewardable_nodes;
    }
}
``` [3](#0-2) 

The code comment even acknowledges this is a known limitation but does not treat it as a bug:

> "If an empty map is sent, the existing values will not be updated, to be consistent with the behavior of `rewardable_nodes`. That behavior may change in the future, so prefer sending None instead of an empty BtreeMap." [4](#0-3) 

Unlike `ipv6`, which has a dedicated `set_ipv6_to_none: Option<bool>` escape hatch to explicitly clear the field, `rewardable_nodes` has no equivalent mechanism. [5](#0-4) 

The `rewardable_nodes` map is the direct input to `get_node_providers_monthly_xdr_rewards`, which computes ICP reward payments to node providers. [6](#0-5) 

---

### Impact Explanation

`rewardable_nodes` directly controls how many nodes of each type a node provider is rewarded for each month. If governance passes a proposal to clear this map (e.g., to stop rewarding a node operator who has been removed or penalized), the proposal executes successfully but the registry state is not updated. The node operator continues to receive monthly ICP rewards they are no longer entitled to. This is a **ledger conservation bug**: ICP is minted and paid out based on stale registry state that governance cannot correct through the normal proposal path.

---

### Likelihood Explanation

The NNS governance canister calls `update_node_operator_config` on the registry canister via the `NnsFunction::UpdateNodeOperatorConfig` proposal type. Any NNS governance participant can submit such a proposal. The bug is triggered whenever the intended update is to clear `rewardable_nodes` to an empty map — a realistic operational action (e.g., decommissioning a node operator). The call succeeds silently, so the proposer has no indication the update failed. The only workaround documented in the code is to "prefer sending None instead of an empty BtreeMap," but `rewardable_nodes` is not `Option`-typed, so `None` cannot be sent for it at all.

---

### Recommendation

1. Change `rewardable_nodes` in `UpdateNodeOperatorConfigPayload` from `BTreeMap<String, u32>` to `Option<BTreeMap<String, u32>>`, consistent with `max_rewardable_nodes`. A `None` value means "do not update"; `Some(empty_map)` means "clear all entries."
2. Update the guard to `if let Some(nodes) = payload.rewardable_nodes { node_operator_record.rewardable_nodes = nodes; }`.
3. Apply the same fix to `max_rewardable_nodes`: remove the inner `is_empty()` guard so `Some(BTreeMap::new())` correctly clears the field.
4. Alternatively, add a `set_rewardable_nodes_to_empty: Option<bool>` field analogous to the existing `set_ipv6_to_none` field as a minimal backward-compatible fix. [7](#0-6) 

---

### Proof of Concept

**Attacker-controlled entry path:**

1. NNS governance passes a proposal of type `UpdateNodeOperatorConfig` with payload:
   ```
   UpdateNodeOperatorConfigPayload {
       node_operator_id: Some(<target_operator>),
       rewardable_nodes: BTreeMap::new(),  // intent: clear all reward entries
       ..Default::default()
   }
   ```
2. The governance canister calls `update_node_operator_config` on the registry canister. [8](#0-7) 
3. `do_update_node_operator_config` is invoked. The guard `if !payload.rewardable_nodes.is_empty()` evaluates to `false` because the map is empty. [1](#0-0) 
4. The assignment is skipped. The `NodeOperatorRecord` is re-encoded and written back with the **original** `rewardable_nodes` intact.
5. The proposal completes with `Executed` status. No error is returned.
6. `get_node_providers_monthly_xdr_rewards` continues to compute rewards using the stale `rewardable_nodes` map, and the node provider continues to receive ICP payments.

The existing unit test `test_should_not_update_fields_if_omitted` explicitly asserts this silent-skip behavior as correct, confirming the bug is present and untested-against for the empty-map-as-clear-intent case. [9](#0-8)

### Citations

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config.rs (L39-49)
```rust
        if let Some(new_allowance) = payload.node_allowance {
            node_operator_record.node_allowance = new_allowance;
        };

        if let Some(new_dc_id) = payload.dc_id {
            node_operator_record.dc_id = new_dc_id;
        }

        if !payload.rewardable_nodes.is_empty() {
            node_operator_record.rewardable_nodes = payload.rewardable_nodes;
        }
```

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config.rs (L75-81)
```rust
        if let Some(max_rewardable_nodes) = payload.max_rewardable_nodes {
            // It might make sense to allow setting this to None, but for now we keep the same
            // behavior as the old field of only making changes if values are set.
            if !max_rewardable_nodes.is_empty() {
                node_operator_record.max_rewardable_nodes = max_rewardable_nodes;
            }
        }
```

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config.rs (L109-111)
```rust
    /// A map from node type to the number of nodes for which the associated
    /// Node Provider should be rewarded.
    pub rewardable_nodes: BTreeMap<String, u32>,
```

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config.rs (L119-122)
```rust
    /// Set the field ipv6 in the NodeOperatorRecord to None. If the field ipv6 in the
    /// UpdateNodeOperatorConfigPayload is set to None, the field ipv6 in the NodeOperatorRecord will
    /// not be updated. This field is for the case when we want to update the value to be None.
    pub set_ipv6_to_none: Option<bool>,
```

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config.rs (L124-130)
```rust
    /// A map from node type to the maximum number of nodes for which the associated Node
    /// Operator could be rewarded.  To set all values to 0, you need to send a map with at least
    /// one entry and a value of 0, like `Some(btreemap! { "type1.1".to_string() => 0 })`.  If an
    /// empty map is sent, the existing values will not be updated, to be consistent with the behavior
    /// of `rewardable_nodes`.  That behavior may change in the future, so prefer sending None
    /// instead of an empty BtreeMap.
    pub max_rewardable_nodes: Option<BTreeMap<String, u32>>,
```

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config.rs (L198-235)
```rust
    #[test]
    fn test_should_not_update_fields_if_omitted() {
        let mut registry = invariant_compliant_registry(0);

        let node_operator_id = PrincipalId::from_str(TEST_NODE_ID).unwrap();

        // create a new NO record
        let node_operator_record = NodeOperatorRecord {
            node_operator_principal_id: node_operator_id.to_vec(),
            node_allowance: 1, // Should be > 0 to add a new node
            node_provider_principal_id: PrincipalId::new_user_test_id(1000).to_vec(),
            dc_id: "DC1".to_string(),
            rewardable_nodes: btreemap! { "type1.1".to_string() => 1 },
            ipv6: Some("foo".to_string()),
            max_rewardable_nodes: btreemap! { "type1.2".to_string() => 1 },
        };
        registry.maybe_apply_mutation_internal(vec![insert(
            make_node_operator_record_key(node_operator_id),
            node_operator_record.encode_to_vec(),
        )]);

        // Make a proposal to upgrade all unassigned nodes to a new version
        let payload = UpdateNodeOperatorConfigPayload {
            node_operator_id: Some(node_operator_id),
            node_allowance: None,
            dc_id: None,
            rewardable_nodes: btreemap! {},
            node_provider_id: None,
            ipv6: None,
            set_ipv6_to_none: None,
            max_rewardable_nodes: None,
        };

        registry.do_update_node_operator_config(payload);
        let updated_node_operator_record = get_node_operator_record(&registry, node_operator_id)
            .expect("Could not find NO Record");
        assert_eq!(updated_node_operator_record, node_operator_record);
    }
```

**File:** rs/registry/canister/src/get_node_providers_monthly_xdr_rewards.rs (L231-246)
```rust
    #[test]
    fn test_get_node_providers_monthly_xdr_rewards_gen1() {
        let registry = registry_init_empty();

        ///////////////////////////////
        // Adding two Node Providers without adding the rewards table yet
        ///////////////////////////////
        let np1 = *TEST_USER1_PRINCIPAL;
        let registry = registry_add_node_operator(
            registry,
            np1,
            np1,
            "NY1".to_string(),
            "North America,US,NY".into(),
            5,
            btreemap! { "type0".to_string() => 4, "type2".to_string() => 1 },
```

**File:** rs/registry/canister/canister/canister.rs (L795-807)
```rust
#[unsafe(export_name = "canister_update update_node_operator_config")]
fn update_node_operator_config() {
    check_caller_is_governance_and_log("update_node_operator_config");
    over(candid_one, |payload: UpdateNodeOperatorConfigPayload| {
        update_node_operator_config_(payload)
    });
}

#[candid_method(update, rename = "update_node_operator_config")]
fn update_node_operator_config_(payload: UpdateNodeOperatorConfigPayload) {
    registry_mut().do_update_node_operator_config(payload);
    recertify_registry();
}
```
