Audit Report

## Title
Default `allow_all` Caller Policy Permits Any Node Operator to Bypass Phased-Rollout Whitelist for `swap_node_in_subnet_directly` — (`rs/registry/canister/src/flags.rs`)

## Summary

`NODE_SWAPPING_CALLERS_POLICY` is initialized to `AccessList::allow_all()` in `flags.rs`, which resolves to a `DenyOnly(HashSet::new())` internally — meaning `is_allowed()` returns `true` for every principal. The canister entry point for `swap_node_in_subnet_directly` performs no caller check of its own, and the internal whitelist check unconditionally passes under the default policy. Any registered node operator can therefore invoke the function and alter live subnet membership without being explicitly authorized, bypassing the intended phased-rollout governance gate.

## Finding Description

`AccessList::allow_all()` is implemented as `DenyOnly(HashSet::new())`, so `is_allowed(&any_principal)` always returns `true`: [1](#0-0) 

The thread-local `NODE_SWAPPING_CALLERS_POLICY` is initialized with this policy: [2](#0-1) 

`IS_NODE_SWAPPING_ENABLED` also defaults to `true`: [3](#0-2) 

The canister entry point has no caller check: [4](#0-3) 

The internal whitelist check calls `is_node_swapping_enabled_for_caller`, which reads from the `allow_all` policy and returns `true` for any caller: [5](#0-4) 

The `RegistryCanisterInitPayload` comment explicitly states these flags "shouldn't be provided when deploying to mainnet," meaning the in-memory defaults in `flags.rs` remain active on mainnet: [6](#0-5) 

The integration test `caller_not_whitelisted` documents the problem directly: it must add a dummy principal to the whitelist to force the policy out of `allow_all` mode, because an empty `swapping_whitelisted_callers` list passed through `AccessList::allow([])` produces `deny_all`, but `None` (the mainnet case) leaves the `allow_all` default intact: [7](#0-6) 

The remaining business-rule checks (node ownership, rate limits, subnet-halted guard) do not compensate: they only prevent cross-operator abuse and do not block a non-whitelisted operator from swapping their own nodes: [8](#0-7) 

The successful swap mutates the subnet's `membership` field and calls `recertify_registry()`, making the change authoritative for all replicas: [9](#0-8) 

## Impact Explanation

Any registered node operator can unilaterally alter live subnet membership in the NNS Registry without a governance proposal, bypassing the phased-rollout access control entirely. Subnet membership changes are certified and propagated to all replicas, so the impact is a concrete, persistent, unauthorized modification to subnet topology. This matches the allowed High impact class: **Application/platform-level DoS, certified-state disruption, or subnet availability impact** — a node operator can introduce an unvetted or degraded node into a production subnet, potentially disrupting consensus or subnet availability. Rate limits (1 swap per subnet per 4 hours) constrain throughput but do not prevent the attack.

## Likelihood Explanation

The exploit path requires only that the attacker be a registered node operator with at least one node currently assigned to a target subnet and one unassigned node — both owned by the same operator. No privileged key, governance majority, or social engineering is required. The call is a standard ingress update to the Registry canister. The `allow_all` default is active unless the canister is explicitly initialized with a non-empty whitelist, and the `init.rs` comment instructs mainnet deployments not to provide these fields, making the vulnerable default the production state.

## Recommendation

1. Change the default policy in `flags.rs` from `AccessList::allow_all()` to `AccessList::deny_all()` so no caller is permitted unless explicitly whitelisted: [2](#0-1) 

2. Add an explicit caller check at the canister entry point (analogous to `check_caller_is_governance_and_log`) before delegating to `do_swap_node_in_subnet_directly`, so access control cannot be silently bypassed by a misconfigured default: [10](#0-9) 

3. Audit `migrate_node_operator_directly` for the same pattern — it also has no caller check at the entry point: [11](#0-10) 

## Proof of Concept

Using the existing integration test harness, install the Registry canister with `RegistryCanisterInitPayloadBuilder::new()` (no explicit whitelist, matching the mainnet default), enable the feature globally and for a target subnet, then call `swap_node_in_subnet_directly` as any node operator who owns both nodes. The call succeeds because `NODE_SWAPPING_CALLERS_POLICY` remains `allow_all()`. The `ensure_feature_is_turned_off` test already demonstrates the harness works; a parallel test with `enable_swapping_feature_globally()` but no `whitelist_swapping_feature_caller()` call would confirm the bypass. The `caller_not_whitelisted` test itself documents that omitting the whitelist entry causes `allow_all` to remain active: [12](#0-11)

### Citations

**File:** rs/nervous_system/access_list/src/lib.rs (L126-130)
```rust
    pub fn allow_all() -> Self {
        Self {
            inner: AccessListInner::DenyOnly(HashSet::new()),
        }
    }
```

**File:** rs/registry/canister/src/flags.rs (L11-11)
```rust
    static IS_NODE_SWAPPING_ENABLED: Cell<bool> = const { Cell::new(true) };
```

**File:** rs/registry/canister/src/flags.rs (L18-18)
```rust
    static NODE_SWAPPING_CALLERS_POLICY: RefCell<AccessList<PrincipalId>> = RefCell::new(AccessList::allow_all());
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

**File:** rs/registry/canister/src/mutations/do_swap_node_in_subnet_directly.rs (L208-224)
```rust
        // Ensure that both of the nodes are owned by the same node operator
        let old_node_operator = PrincipalId::try_from(old_node.node_operator_id).unwrap();
        let new_node_operator = PrincipalId::try_from(new_node.node_operator_id).unwrap();

        if old_node_operator != new_node_operator {
            return Err(SwapError::NodesOwnedByDifferentOperators);
        }

        // Ensure that the caller is the actual node operator of the nodes.
        // Since the before check passed we can check for either one of the
        // node operators, new or old.
        if new_node_operator != caller {
            return Err(SwapError::CallerNodeOperatorMismatch {
                caller,
                node_operator: new_node_operator,
            });
        }
```

**File:** rs/registry/canister/src/init.rs (L9-18)
```rust
    // IC-1869 (Node swaps) flags that are used
    // integration tests and will be removed as
    // a part of Phase 3 of the rollout.
    //
    // Note: in `src/flags.rs` are the default
    // values for all of these arguments and these
    // shouldn't be provided when deploying to
    // mainnet and should be left behind the
    // test configuration.
    //
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
