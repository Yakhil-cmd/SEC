### Title
Empty Caller Whitelist Permits Any Ingress Sender to Swap Subnet Nodes via `swap_node_in_subnet_directly` - (File: rs/registry/canister/canister/canister.rs)

---

### Summary

The `swap_node_in_subnet_directly` update method on the NNS Registry canister has no canister-level caller restriction. Its sole access control is an internal caller whitelist (`is_node_swapping_enabled_for_caller`). Integration test evidence demonstrates that when the whitelist is empty, the feature is enabled for **all** callers — meaning any unprivileged ingress sender can swap nodes in and out of subnets without authorization, directly mutating subnet membership in the Registry.

---

### Finding Description

The canister entry point `swap_node_in_subnet_directly` carries no `check_caller_is_governance_and_log` or equivalent guard:

```rust
#[unsafe(export_name = "canister_update swap_node_in_subnet_directly")]
fn swap_node_in_subnet_directly() {
    over(candid_one, |payload: SwapNodeInSubnetDirectlyPayload| {
        swap_node_in_subnet_directly_(payload)
    });
}
``` [1](#0-0) 

Compare this to the adjacent `update_node_operator_config` which explicitly calls `check_caller_is_governance_and_log`: [2](#0-1) 

The internal implementation delegates to `swap_nodes_inner`, which calls `swapping_enabled_for_caller`: [3](#0-2) 

The caller check is: [4](#0-3) 

The critical evidence of the empty-whitelist behavior is in the integration test `caller_not_whitelisted`:

```rust
// In order to avoid the feature being enabled for all node
// operators there needs to be some other caller whitelisted.
builder.whitelist_swapping_feature_caller(PrincipalId::new_user_test_id(999));
``` [5](#0-4) 

This comment is unambiguous: **if no caller is whitelisted (empty whitelist), the feature is enabled for all node operators**. The test must whitelist an unrelated principal (999) to make the whitelist non-empty, so that the actual test caller (user 1) is correctly rejected. If empty whitelist meant "reject all," whitelisting user 999 would be unnecessary.

The successful swap mutates the Registry's subnet membership record, writing the new node into the subnet and removing the old one: [6](#0-5) 

---

### Impact Explanation

An unprivileged ingress sender who can reach the NNS Registry canister can call `swap_node_in_subnet_directly` when the caller whitelist is empty. A successful call:

1. Removes a legitimate node from a subnet's membership record in the Registry.
2. Inserts an attacker-controlled or arbitrary node into the subnet.
3. Triggers `recertify_registry()`, making the tampered state certified and propagated to all replicas.

This constitutes a **governance authorization bypass** and a **registry state integrity violation**: subnet membership is normally changed only via NNS governance proposals, but this path bypasses governance entirely. Disrupting subnet membership can degrade or halt consensus on the targeted subnet.

---

### Likelihood Explanation

The feature is currently disabled by default (the test `ensure_feature_is_turned_off` confirms this). However, the vulnerability is latent and becomes exploitable the moment the feature is enabled globally without simultaneously populating the caller whitelist. This is a realistic operational scenario: a governance proposal could enable the feature flag before the whitelist is configured, creating a window during which any ingress sender can exploit the open entry point. The attacker entry path is a standard signed ingress message to the NNS Registry canister — no privileged access required.

---

### Recommendation

1. **Add a canister-level guard** at the `swap_node_in_subnet_directly` entry point that rejects all callers not in the whitelist before entering the internal logic, mirroring the pattern used by `check_caller_is_governance_and_log`.
2. **Invert the whitelist default**: treat an empty whitelist as "deny all" rather than "allow all." The current behavior (empty = allow all) is the opposite of a safe default.
3. **Atomically enable the feature and populate the whitelist** in any governance proposal that activates node swapping, so there is never a window where the feature is on but the whitelist is empty.

---

### Proof of Concept

**Precondition**: The node swapping feature is enabled globally and for a target subnet, but no callers have been whitelisted (whitelist is empty).

**Attacker steps**:

1. Identify a node `old_node_id` that is a member of a target subnet, and a node `new_node_id` owned by the same node operator (required by `validate_node_swap`).
2. Send a signed ingress update call to the NNS Registry canister (`rrkah-fqaaa-aaaaa-aaaaq-cai` or equivalent) with method `swap_node_in_subnet_directly` and payload:
   ```
   { old_node_id: <target_subnet_node>, new_node_id: <replacement_node> }
   ```
3. Because the whitelist is empty, `is_node_swapping_enabled_for_caller` returns `true` for any caller, bypassing the whitelist check.
4. `validate_node_swap` passes if the attacker controls both nodes (or can identify two nodes owned by the same operator).
5. The Registry writes the swap mutation and calls `recertify_registry()`, permanently altering subnet membership without any governance proposal. [7](#0-6) [8](#0-7)

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

**File:** rs/registry/canister/src/mutations/do_swap_node_in_subnet_directly.rs (L112-147)
```rust
    pub fn do_swap_node_in_subnet_directly(&mut self, payload: SwapNodeInSubnetDirectlyPayload) {
        self.swap_nodes_inner(payload, dfn_core::api::caller(), now_system_time())
            .unwrap_or_else(|e| panic!("{e}"));
    }

    /// Top level function for the swapping feature which has all inputs.
    fn swap_nodes_inner(
        &mut self,
        payload: SwapNodeInSubnetDirectlyPayload,
        caller: PrincipalId,
        now: SystemTime,
    ) -> Result<(), SwapError> {
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

**File:** rs/registry/canister/tests/swap_node_in_subnet_directly.rs (L169-193)
```rust
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
