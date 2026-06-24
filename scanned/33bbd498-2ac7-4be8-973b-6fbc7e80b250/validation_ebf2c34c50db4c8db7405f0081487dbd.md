### Title
Node Provider Can Reassign Node Operator Record to Any Arbitrary Unregistered Principal Without Governance Approval - (`rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs`)

---

### Summary

The `update_node_operator_config_directly` endpoint in the Registry canister allows any current Node Provider to reassign the `node_provider_principal_id` of their own Node Operator record to **any arbitrary principal**, including one that has never been registered as a Node Provider through NNS governance. This mirrors the Wildcat H-05 pattern: a function that should enforce a whitelist of valid target entities instead accepts any caller-supplied principal, allowing a Node Provider facing sanctions or removal to transfer their Node Operator record to a fresh, unregistered identity and escape accountability.

---

### Finding Description

The canister entry point is explicitly annotated as open to all callers:

```rust
// This method can be called by anyone
``` [1](#0-0) 

The underlying implementation `do_update_node_operator_config_directly_` does verify that the caller equals the current `node_provider_principal_id` stored in the record: [2](#0-1) 

However, once that check passes, the function unconditionally writes the caller-supplied `node_provider_id` — which can be **any** `PrincipalId` — directly into the registry record: [3](#0-2) 

The `UpdateNodeOperatorConfigDirectlyPayload` struct accepts any `PrincipalId` for the new provider field with no constraint that it must correspond to a principal already registered as a Node Provider through NNS governance: [4](#0-3) 

The unit test confirms this: `new_np_id = PrincipalId::new_user_test_id(10_001)` — a principal that was never registered as a Node Provider — is accepted without error: [5](#0-4) 

The only other guard is a rate limiter, which throttles frequency but does not prevent the transfer itself: [6](#0-5) 

The contrast with the governance-gated variant `update_node_operator_config` is stark — that method enforces `check_caller_is_governance_and_log`: [7](#0-6) 

---

### Impact Explanation

A Node Provider whose principal is being investigated, whose NNS Node Provider record is being proposed for removal, or who anticipates a governance action against them can:

1. Call `update_node_operator_config_directly` to transfer the `node_provider_principal_id` of their Node Operator record to a fresh, unregistered principal they control.
2. The new principal immediately inherits ownership of the Node Operator record and all associated nodes.
3. The new principal is not a registered Node Provider in the NNS, making it harder for governance to track, attribute, or sanction the entity.
4. The new principal can repeat the transfer to yet another fresh principal, creating an indefinite chain of reassignments.

This is the direct IC analog of the Wildcat H-05 pattern: just as a sanctioned lender could move market tokens to fresh accounts and grant them `WithdrawOnly` to exit the market, a Node Provider can move their Node Operator record to a fresh principal to escape governance accountability — all without any NNS proposal.

---

### Likelihood Explanation

The attack path requires only a valid ingress message from the current Node Provider's identity. No privileged access, no threshold corruption, and no social engineering is needed. The rate limiter imposes a delay between operations but does not block the transfer. Any Node Provider who anticipates a governance action against them has a clear, low-effort path to execute this before the proposal passes.

---

### Recommendation

The `do_update_node_operator_config_directly_` function should validate that the supplied `node_provider_id` corresponds to a principal already registered as a Node Provider in the NNS registry before writing the mutation. Alternatively, the ability to change `node_provider_principal_id` should be removed from this direct-call path entirely and reserved for the governance-gated `update_node_operator_config` proposal flow, consistent with how all other sensitive Node Operator fields are managed.

---

### Proof of Concept

1. Node Provider NP-A controls Node Operator record NO-1 (`node_provider_principal_id = NP-A`).
2. NNS governance initiates a proposal to remove NP-A's Node Provider record.
3. Before the proposal passes, NP-A calls `update_node_operator_config_directly` with:
   - `node_operator_id = NO-1`
   - `node_provider_id = NP-B` (a fresh, never-registered principal controlled by NP-A)
4. The registry writes `node_provider_principal_id = NP-B` for NO-1.
5. NP-A's original principal is now disconnected from NO-1; NP-B controls it.
6. The governance proposal to remove NP-A has no effect on NO-1 or its nodes.
7. NP-B can repeat step 3 with NP-C if NNS governance targets NP-B. [8](#0-7) [9](#0-8)

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

**File:** rs/registry/canister/canister/canister.rs (L809-829)
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

#[candid_method(update, rename = "update_node_operator_config_directly")]
fn update_node_operator_config_directly_(payload: UpdateNodeOperatorConfigDirectlyPayload) {
    registry_mut().do_update_node_operator_config_directly(payload);
    recertify_registry();
}
```

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs (L58-100)
```rust
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

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs (L106-116)
```rust
#[derive(Clone, Eq, PartialEq, CandidType, Deserialize, Message, Serialize)]
pub struct UpdateNodeOperatorConfigDirectlyPayload {
    /// The principal id of the node operator. This principal is the entity that
    /// is able to add and remove nodes.
    #[prost(message, optional, tag = "1")]
    pub node_operator_id: Option<PrincipalId>,

    /// The principal id of this node's provider.
    #[prost(message, optional, tag = "2")]
    pub node_provider_id: Option<PrincipalId>,
}
```

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs (L148-169)
```rust
        let new_np_id = PrincipalId::new_user_test_id(10_001);
        let request = UpdateNodeOperatorConfigDirectlyPayload {
            node_operator_id: Some(node_operator_id),
            node_provider_id: Some(new_np_id),
        };

        // The original node provider should be able to change the node operator configuration.
        let caller = node_provider_id;

        registry
            .do_update_node_operator_config_directly_(request, caller, now)
            .unwrap();

        assert_eq!(
            PrincipalId::try_from(
                get_node_operator_record(&registry, node_operator_id)
                    .unwrap()
                    .node_provider_principal_id
            )
            .unwrap(),
            new_np_id
        );
```
