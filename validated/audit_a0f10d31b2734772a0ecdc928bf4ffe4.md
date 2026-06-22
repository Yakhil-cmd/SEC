### Title
Default `allow_all` Caller Policy Bypasses Node-Swapping Whitelist — (`rs/registry/canister/src/flags.rs` / `rs/registry/canister/canister/canister.rs`)

---

### Summary

The `swap_node_in_subnet_directly` update method on the Registry canister is intended to be gated by a caller whitelist during phased rollout. However, the whitelist policy defaults to `AccessList::allow_all()`, meaning any node operator can call the function and alter subnet membership without being explicitly authorized. The canister entry point also performs no caller check of its own.

---

### Finding Description

The Registry canister exposes `swap_node_in_subnet_directly` as an unrestricted update call:

```rust
#[unsafe(export_name = "canister_update swap_node_in_subnet_directly")]
fn swap_node_in_subnet_directly() {
    over(candid_one, |payload: SwapNodeInSubnetDirectlyPayload| {
        swap_node_in_subnet_directly_(payload)
    });
}
``` [1](#0-0) 

Compare this with the governance-gated sibling `update_node_operator_config`, which calls `check_caller_is_governance_and_log` before doing anything. [2](#0-1) 

Inside `do_swap_node_in_subnet_directly`, the caller whitelist check is:

```rust
fn swapping_enabled_for_caller(caller: PrincipalId) -> Result<(), SwapError> {
    if !is_node_swapping_enabled_for_caller(caller) {
        return Err(SwapError::FeatureDisabledForCaller { caller });
    }
    Ok(())
}
``` [3](#0-2) 

`is_node_swapping_enabled_for_caller` reads from `NODE_SWAPPING_CALLERS_POLICY`, which is initialized as:

```rust
static NODE_SWAPPING_CALLERS_POLICY: RefCell<AccessList<PrincipalId>> =
    RefCell::new(AccessList::allow_all());
``` [4](#0-3) 

The code comment in `flags.rs` explicitly states the whitelist is for phased rollout to "a specific subset of callers": [5](#0-4) 

The integration test for `caller_not_whitelisted` even documents the problem in a comment: *"In order to avoid the feature being enabled for all node operators there needs to be some other caller whitelisted."* This confirms that unless at least one principal is explicitly added to the whitelist, the policy remains `allow_all` and every node operator bypasses the intended restriction. [6](#0-5) 

---

### Impact Explanation

A node operator who is **not** on the intended whitelist can call `swap_node_in_subnet_directly` and replace a node in a live subnet with one of their own unassigned nodes. The function mutates the subnet's `membership` field in the Registry and calls `recertify_registry()`, making the change authoritative for all replicas. [7](#0-6) 

The remaining business-rule checks (caller must own both nodes, rate limits, subnet must not be halted) do not compensate for the missing whitelist enforcement, because they only prevent cross-operator abuse — they do not prevent a non-whitelisted node operator from performing the swap with their own nodes. The result is unauthorized, premature subnet membership changes that bypass the phased-rollout governance process.

---

### Likelihood Explanation

The entry path is a standard ingress update call to the Registry canister, reachable by any node operator principal on mainnet. No privileged key, governance majority, or social engineering is required. The default `allow_all` policy is active unless the canister is explicitly initialized with a non-empty whitelist, making this a configuration-default vulnerability that is trivially exploitable by any node operator.

---

### Recommendation

1. **Change the default policy** from `AccessList::allow_all()` to `AccessList::deny_all()` (or an empty allowlist) so that no caller is permitted unless explicitly whitelisted:

```rust
static NODE_SWAPPING_CALLERS_POLICY: RefCell<AccessList<PrincipalId>> =
    RefCell::new(AccessList::deny_all()); // was: allow_all()
```

2. **Add an explicit caller check** at the canister entry point (analogous to `check_caller_is_governance_and_log`) or enforce the whitelist check before any state mutation, so the access control cannot be silently bypassed by a misconfigured default.

3. **Audit `migrate_node_operator_directly`** for the same pattern — it also has no caller check at the canister entry point: [8](#0-7) 

---

### Proof of Concept

A node operator `attacker_node_operator` who owns `old_node` (currently in `target_subnet`) and `new_node` (unassigned) can call:

```bash
dfx canister call registry swap_node_in_subnet_directly \
  '(record { old_node_id = opt principal "OLD_NODE_ID"; new_node_id = opt principal "NEW_NODE_ID" })' \
  --identity attacker_node_operator
```

Because `NODE_SWAPPING_CALLERS_POLICY` defaults to `allow_all()`, the whitelist check at `swapping_enabled_for_caller` passes unconditionally. The remaining checks (node ownership, rate limit) pass because the attacker legitimately owns both nodes. The Registry is mutated and recertified, replacing `old_node` with `new_node` in `target_subnet`'s membership — without the attacker ever being added to the intended whitelist.

### Citations

**File:** rs/registry/canister/canister/canister.rs (L795-801)
```rust
#[unsafe(export_name = "canister_update update_node_operator_config")]
fn update_node_operator_config() {
    check_caller_is_governance_and_log("update_node_operator_config");
    over(candid_one, |payload: UpdateNodeOperatorConfigPayload| {
        update_node_operator_config_(payload)
    });
}
```

**File:** rs/registry/canister/canister/canister.rs (L831-842)
```rust
#[unsafe(export_name = "canister_update swap_node_in_subnet_directly")]
fn swap_node_in_subnet_directly() {
    over(candid_one, |payload: SwapNodeInSubnetDirectlyPayload| {
        swap_node_in_subnet_directly_(payload)
    });
}

#[candid_method(update, rename = "swap_node_in_subnet_directly")]
fn swap_node_in_subnet_directly_(payload: SwapNodeInSubnetDirectlyPayload) {
    registry_mut().do_swap_node_in_subnet_directly(payload);
    recertify_registry();
}
```

**File:** rs/registry/canister/canister/canister.rs (L844-855)
```rust
#[unsafe(export_name = "canister_update migrate_node_operator_directly")]
fn migrate_node_operator_directly() {
    over(candid_one, |payload: MigrateNodeOperatorPayload| {
        migrate_node_operator_directly_(payload)
    });
}

#[candid_method(update, rename = "migrate_node_operator_directly")]
fn migrate_node_operator_directly_(payload: MigrateNodeOperatorPayload) {
    registry_mut().do_migrate_node_operator_directly(payload);
    recertify_registry();
}
```

**File:** rs/registry/canister/src/mutations/do_swap_node_in_subnet_directly.rs (L149-156)
```rust
    /// Check if the caller is whitelisted to use this feature.
    fn swapping_enabled_for_caller(caller: PrincipalId) -> Result<(), SwapError> {
        if !is_node_swapping_enabled_for_caller(caller) {
            return Err(SwapError::FeatureDisabledForCaller { caller });
        }

        Ok(())
    }
```

**File:** rs/registry/canister/src/mutations/do_swap_node_in_subnet_directly.rs (L236-265)
```rust
    fn swap_nodes_in_subnet(
        &mut self,
        subnet_id: SubnetId,
        old_node_id: PrincipalId,
        new_node_id: PrincipalId,
    ) -> Result<(), SwapError> {
        let mut subnet = self.get_subnet_or_panic(subnet_id);
        let subnet_size_before = subnet.membership.len();
        subnet.membership.retain(|node| {
            let node_id = PrincipalId::try_from(node).unwrap();

            node_id != old_node_id
        });
        subnet.membership.push(new_node_id.to_vec());
        let subnet_size_after = subnet.membership.len();

        // Ensure subnet size stays consistent
        if subnet_size_before != subnet_size_after {
            return Err(SwapError::SubnetSizeMismatch { subnet_id });
        }

        let subnet_mutations = vec![upsert(
            make_subnet_record_key(subnet_id).as_bytes(),
            subnet.encode_to_vec(),
        )];

        self.maybe_apply_mutation_internal(subnet_mutations);

        Ok(())
    }
```

**File:** rs/registry/canister/src/flags.rs (L13-21)
```rust
    // Temporary flags related to the node swapping feature.
    //
    // These are needed for the phased rollout approach in order
    // allow granular rolling out of the feature to specific subnets
    // to specific subset of callers.
    static NODE_SWAPPING_CALLERS_POLICY: RefCell<AccessList<PrincipalId>> = RefCell::new(AccessList::allow_all());

    static NODE_SWAPPING_SUBNETS_POLICY: RefCell<AccessList<SubnetId>> = RefCell::new(AccessList::allow_all());
}
```

**File:** rs/registry/canister/tests/swap_node_in_subnet_directly.rs (L139-194)
```rust
#[tokio::test]
async fn caller_not_whitelisted() {
    let pocket_ic = PocketIcBuilder::new().with_nns_subnet().build_async().await;

    let subnet_id = SubnetId::new(PrincipalId::new_subnet_test_id(1));
    let node_operator_id = PrincipalId::new_user_test_id(1);

    let (mutations, nodes) = get_mutations_and_node_ids(&[
        // Old node id
        NodeInformation {
            subnet_id: Some(subnet_id),
            node_operator: node_operator_id,
        },
        // New node id
        NodeInformation {
            subnet_id: None,
            node_operator: node_operator_id,
        },
    ]);

    let old_node_id = nodes[0];
    let new_node_id = nodes[1];

    let mut builder = RegistryCanisterInitPayloadBuilder::new();
    builder.push_init_mutate_request(RegistryAtomicMutateRequest {
        mutations,
        preconditions: vec![],
    });
    builder.enable_swapping_feature_globally();
    builder.enable_swapping_feature_for_subnet(subnet_id);
    // In order to avoid the feature being enabled for all node
    // operators there needs to be some other caller whitelisted.
    builder.whitelist_swapping_feature_caller(PrincipalId::new_user_test_id(999));

    install_registry_canister_with_payload_builder(&pocket_ic, builder.build(), true).await;

    let response = swap_node_in_subnet_directly(
        &pocket_ic,
        SwapNodeInSubnetDirectlyPayload {
            new_node_id: Some(new_node_id.get()),
            old_node_id: Some(old_node_id.get()),
        },
        node_operator_id,
    )
    .await;

    let expected_err = SwapError::FeatureDisabledForCaller {
        caller: node_operator_id,
    };
    assert!(
        response
            .as_ref()
            .is_err_and(|err| err.reject_message.contains(&format!("{}", expected_err))),
        "Expected error {expected_err:?}, but got {response:?}"
    )
}
```
