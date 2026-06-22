### Title
Registry Canister `swap_node_in_subnet_directly` Caller Whitelist Defaults to Allow-All, Permitting Any Node Operator to Modify Subnet Membership — (File: `rs/registry/canister/src/mutations/do_swap_node_in_subnet_directly.rs`)

### Summary

The `swap_node_in_subnet_directly` endpoint in the registry canister exposes a governance authorization bug: its caller whitelist defaults to "allow all" when unconfigured (empty). Any node operator — not just explicitly approved ones — can invoke this endpoint to modify live subnet membership and exhaust a shared in-canister rate limiter, affecting all other node operators on the same subnet.

### Finding Description

The canister entry point `canister_update swap_node_in_subnet_directly` carries no outer governance authorization check, unlike the majority of registry mutation endpoints: [1](#0-0) 

Compare this to adjacent endpoints that explicitly gate on governance: [2](#0-1) 

The inner implementation delegates to `swap_nodes_inner`, which calls `swapping_enabled_for_caller`: [3](#0-2) 

The critical behavior is documented in the integration test for the `caller_not_whitelisted` scenario: [4](#0-3) 

The comment at line 170–171 explicitly states: *"In order to avoid the feature being enabled for all node operators there needs to be some other caller whitelisted."* This confirms that when the whitelist is empty (the default/unconfigured state), `is_node_swapping_enabled_for_caller` returns `true` for **every** caller. Only when at least one principal is explicitly added to the whitelist does the restriction activate.

Additionally, the rate limiter that tracks swap capacity is a canister-global thread-local shared across all callers: [5](#0-4) 

A single swap on a subnet consumes the entire subnet capacity slot for `NODE_SWAPS_SUBNET_CAPACITY_INTERVAL` (4 hours), blocking all other operators on that subnet: [6](#0-5) 

### Impact Explanation

When the node-swapping feature is globally enabled and at least one subnet is whitelisted, but no caller whitelist has been configured:

1. **Any node operator** (not just explicitly approved ones) can call `swap_node_in_subnet_directly` and alter live subnet membership by replacing one of their own nodes with another they own.
2. **Shared rate limiter exhaustion**: After a successful swap, the subnet's 4-hour capacity slot is consumed. All other node operators — including legitimately whitelisted ones — are blocked from performing swaps on that subnet until the interval expires. A malicious operator can repeat this every 4 hours to maintain the denial.
3. **Subnet membership integrity**: Subnet membership changes are certified registry state consumed by consensus. Unauthorized membership changes can affect subnet performance and node reward accounting.

This is a direct analog to `fil_configure`: any caller can modify shared state (subnet membership / rate limiter) that affects other callers, without being explicitly authorized.

### Likelihood Explanation

**Medium.** The preconditions are:
- The swapping feature must be globally enabled (not the default — requires explicit activation).
- At least one subnet must be whitelisted for swapping.
- The caller whitelist must be unconfigured (the default state before any operator is explicitly added).

The default state of a newly configured swapping deployment satisfies all three conditions. A node operator who is aware of this behavior can exploit it immediately upon feature activation, before the operator list is populated.

### Recommendation

Change the semantics of the caller whitelist so that an **empty list means deny all**, not allow all. The current "empty = unrestricted" default inverts the expected security posture of an allowlist. Concretely, `is_node_swapping_enabled_for_caller` should return `false` when the whitelist is empty (not configured), and only return `true` when the caller is explicitly present in a non-empty list. This matches the behavior already demonstrated in the unit test via `test_set_swapping_whitelisted_callers(vec![])`: [7](#0-6) 

### Proof of Concept

1. Deploy the registry canister with the swapping feature globally enabled and one subnet whitelisted, but with **no callers explicitly whitelisted** (the default state).
2. As any node operator who owns a node on the whitelisted subnet and an unassigned spare node, call `swap_node_in_subnet_directly` with `old_node_id` = the assigned node and `new_node_id` = the spare.
3. Observe that the call succeeds despite the caller never being added to the whitelist — confirmed by the integration test comment at line 170–171 of `rs/registry/canister/tests/swap_node_in_subnet_directly.rs`.
4. Observe that the subnet's 4-hour rate limit slot is now consumed, blocking all other node operators from swapping on that subnet until the interval expires. [8](#0-7)

### Citations

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

**File:** rs/registry/canister/src/mutations/do_swap_node_in_subnet_directly.rs (L43-58)
```rust
impl SwapRateLimiter {
    fn new() -> Self {
        Self {
            subnet_limiter: InMemoryRateLimiter::new_in_memory(RateLimiterConfig {
                add_capacity_amount: 1,
                add_capacity_interval: NODE_SWAPS_SUBNET_CAPACITY_INTERVAL,
                max_capacity: 1,
                max_reservations: 1,
            }),
            node_operator_limiter: InMemoryRateLimiter::new_in_memory(RateLimiterConfig {
                add_capacity_amount: 1,
                add_capacity_interval: NODE_SWAPS_NODE_OPERATOR_CAPACITY_INTERVAL,
                max_capacity: 1,
                max_reservations: 1,
            }),
        }
```

**File:** rs/registry/canister/src/mutations/do_swap_node_in_subnet_directly.rs (L106-108)
```rust
thread_local! {
    static SWAP_LIMITER: RefCell<SwapRateLimiter> = RefCell::new(SwapRateLimiter::new());
}
```

**File:** rs/registry/canister/src/mutations/do_swap_node_in_subnet_directly.rs (L124-156)
```rust
        // Check if the feature is enabled on the network.
        if !is_node_swapping_enabled() {
            return Err(SwapError::FeatureDisabled);
        }

        // Check if the payload is valid by itself.
        payload.validate()?;
        let (old_node_id, new_node_id) =
            (payload.old_node_id.unwrap(), payload.new_node_id.unwrap());

        //Check if the feature is allowed on the target subnet and for the caller
        Self::swapping_enabled_for_caller(caller)?;
        let subnet_id = self.find_subnet_for_old_node(old_node_id)?;
        Self::swapping_allowed_on_subnet(subnet_id)?;

        let reservation =
            SWAP_LIMITER.with_borrow_mut(|limiter| limiter.try_reserve(caller, subnet_id, now))?;

        self.validate_node_swap(old_node_id, new_node_id, caller, subnet_id)?;
        self.swap_nodes_in_subnet(subnet_id, old_node_id, new_node_id)?;

        SWAP_LIMITER.with_borrow_mut(|limiter| limiter.commit(reservation, now));
        Ok(())
    }

    /// Check if the caller is whitelisted to use this feature.
    fn swapping_enabled_for_caller(caller: PrincipalId) -> Result<(), SwapError> {
        if !is_node_swapping_enabled_for_caller(caller) {
            return Err(SwapError::FeatureDisabledForCaller { caller });
        }

        Ok(())
    }
```

**File:** rs/registry/canister/tests/swap_node_in_subnet_directly.rs (L139-193)
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
```
