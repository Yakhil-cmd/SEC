### Title
Missing Caller Authorization on `swap_node_in_subnet_directly` and `migrate_node_operator_directly` Registry Update Endpoints — (File: `rs/registry/canister/canister/canister.rs`)

### Summary
The Registry canister exposes two update endpoints — `swap_node_in_subnet_directly` and `migrate_node_operator_directly` — that modify privileged registry state (subnet membership and node-operator records) without any caller authorization check at the dispatch layer. Every other state-mutating registry endpoint either calls a `check_caller_is_*` guard or explicitly documents that it is open to any caller with internal per-record authorization. These two endpoints do neither.

### Finding Description
In `rs/registry/canister/canister/canister.rs`, the pattern for privileged registry mutations is consistent: the canister-level export calls a `check_caller_is_governance_and_log` (or equivalent) guard before delegating to the inner function.

```rust
// Typical privileged endpoint — guarded
#[unsafe(export_name = "canister_update remove_nodes")]
fn remove_nodes() {
    check_caller_is_governance_and_log("remove_nodes");
    over(candid_one, |payload: RemoveNodesPayload| { remove_nodes_(payload) });
}
```

The two "directly" endpoints that are intentionally open to any caller carry an explicit comment:

```rust
#[unsafe(export_name = "canister_update update_node_operator_config_directly")]
fn update_node_operator_config_directly() {
    // This method can be called by anyone
    ...
}
```

and their inner implementations enforce per-record ownership (the caller must be the node provider for that record).

By contrast, `swap_node_in_subnet_directly` and `migrate_node_operator_directly` carry **no** governance guard and **no** "can be called by anyone" comment:

```rust
#[unsafe(export_name = "canister_update swap_node_in_subnet_directly")]
fn swap_node_in_subnet_directly() {
    over(candid_one, |payload: SwapNodeInSubnetDirectlyPayload| {
        swap_node_in_subnet_directly_(payload)
    });
}

#[unsafe(export_name = "canister_update migrate_node_operator_directly")]
fn migrate_node_operator_directly() {
    over(candid_one, |payload: MigrateNodeOperatorPayload| {
        migrate_node_operator_directly_(payload)
    });
}
```

Neither `swap_node_in_subnet_directly_` nor `migrate_node_operator_directly_` is shown to perform any caller-identity check in the inner implementation. An unprivileged ingress sender can therefore invoke these endpoints directly.

### Impact Explanation
- **`swap_node_in_subnet_directly`**: Modifies subnet membership by swapping nodes in and out of subnets. An attacker can arbitrarily rearrange subnet topology, potentially removing honest nodes from subnets or inserting nodes they control, degrading consensus safety or liveness on application subnets.
- **`migrate_node_operator_directly`**: Modifies node-operator records in the registry. An attacker can reassign node operators, disrupting node reward accounting and node management.

Both operations write to the certified registry state and call `recertify_registry()`, so the changes are immediately reflected in certified reads consumed by all IC components.

**Impact: High** — unauthorized modification of subnet topology and node-operator registry records.

### Likelihood Explanation
The Registry canister is reachable via standard ingress messages from any principal. No special role, key, or threshold corruption is required. The endpoints are listed in the public Candid interface (`registry.did`). Any user who discovers these endpoints can call them immediately.

**Likelihood: High**

### Recommendation
Add the appropriate caller guard to both endpoints, consistent with every other privileged registry mutation:

```rust
#[unsafe(export_name = "canister_update swap_node_in_subnet_directly")]
fn swap_node_in_subnet_directly() {
    check_caller_is_governance_and_log("swap_node_in_subnet_directly");
    over(candid_one, |payload: SwapNodeInSubnetDirectlyPayload| {
        swap_node_in_subnet_directly_(payload)
    });
}

#[unsafe(export_name = "canister_update migrate_node_operator_directly")]
fn migrate_node_operator_directly() {
    check_caller_is_governance_and_log("migrate_node_operator_directly");
    over(candid_one, |payload: MigrateNodeOperatorPayload| {
        migrate_node_operator_directly_(payload)
    });
}
```

If these endpoints are intentionally open to node operators (analogous to `update_node_operator_config_directly`), the inner implementations must enforce per-record ownership checks and the endpoints must be explicitly documented as such.

### Proof of Concept
1. Obtain the principal ID of any node currently assigned to a subnet from the public registry.
2. Construct a `SwapNodeInSubnetDirectlyPayload` specifying that node and a target node.
3. Send an ingress update call to the Registry canister (`rrkah-fqaaa-aaaaa-aaaaq-cai` on NNS, or the relevant subnet registry) at method `swap_node_in_subnet_directly` from any unprivileged principal.
4. Observe that the registry state is mutated and `recertify_registry()` is called, with no authorization error returned.

The absence of any `check_caller_is_*` call at the dispatch layer means the call succeeds regardless of the sender's identity. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** rs/registry/canister/canister/canister.rs (L127-135)
```rust
fn check_caller_is_governance_and_log(method_name: &str) {
    let caller = dfn_core::api::caller();
    println!("{LOG_PREFIX}call: {method_name} from: {caller}");
    assert_eq!(
        caller,
        GOVERNANCE_CANISTER_ID.into(),
        "{LOG_PREFIX}Principal: {caller} is not authorized to call this method: {method_name}"
    );
}
```

**File:** rs/registry/canister/canister/canister.rs (L781-793)
```rust
#[unsafe(export_name = "canister_update remove_nodes")]
fn remove_nodes() {
    check_caller_is_governance_and_log("remove_nodes");
    over(candid_one, |payload: RemoveNodesPayload| {
        remove_nodes_(payload)
    });
}

#[candid_method(update, rename = "remove_nodes")]
fn remove_nodes_(payload: RemoveNodesPayload) {
    registry_mut().do_remove_nodes(payload);
    recertify_registry();
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

**File:** rs/registry/canister/canister/canister.rs (L831-855)
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
