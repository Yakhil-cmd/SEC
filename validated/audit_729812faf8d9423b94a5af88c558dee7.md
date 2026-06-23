### Title
Missing Caller Authorization on Critical Registry Mutation Endpoints — (`File: rs/registry/canister/canister/canister.rs`)

---

### Summary

The Registry canister exposes two `canister_update` endpoints — `swap_node_in_subnet_directly` and `migrate_node_operator_directly` — that directly mutate the IC's authoritative network registry and recertify it, but perform **no caller authorization check whatsoever**. Any unprivileged ingress sender or canister can invoke these methods to alter subnet topology and node-operator assignments, bypassing the NNS governance proposal process entirely.

---

### Finding Description

In `rs/registry/canister/canister/canister.rs`, the two endpoints are defined as:

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
``` [1](#0-0) 

Neither function calls any of the authorization helpers used by every other state-mutating endpoint in the same file, such as `check_caller_is_governance_and_log`, `check_caller_is_governance_or_engine_controller_and_log`, or `check_caller_is_canister_migration_orchestrator_and_log`. [2](#0-1) 

For contrast, every other critical registry mutation endpoint in the same file enforces one of these checks before proceeding: [3](#0-2) 

Even the intentionally open `update_node_operator_config_directly` — which carries an explicit comment "This method can be called by anyone" — still validates internally that the caller is the node provider principal for the targeted record: [4](#0-3) 

`swap_node_in_subnet_directly` and `migrate_node_operator_directly` have no equivalent internal check.

---

### Impact Explanation

The IC Registry canister is the single source of truth for the entire network's configuration. `recertify_registry()` is called after each mutation, meaning the tampered state is immediately certified and propagated to all subnets.

An attacker can:
- **Swap arbitrary nodes between subnets** via `swap_node_in_subnet_directly`, disrupting subnet membership, potentially removing honest nodes from security-critical subnets (e.g., the NNS subnet or fiduciary subnet), or inserting nodes into subnets they do not belong to.
- **Migrate node-operator records** via `migrate_node_operator_directly`, corrupting the node-operator-to-node-provider mapping used for reward calculations and operational accountability.

Both actions bypass the NNS governance proposal process that is the intended and only authorized path for such changes.

---

### Likelihood Explanation

The Registry canister is reachable via standard ingress messages from any principal. No special privilege, leaked key, or social engineering is required. The endpoints are exported under their standard Candid names and are callable by any user or canister on the IC. The attack requires only knowledge of a valid `SwapNodeInSubnetDirectlyPayload` or `MigrateNodeOperatorPayload`, both of which are defined in public protobuf schemas.

---

### Recommendation

Add the appropriate authorization guard as the first statement in both `swap_node_in_subnet_directly` and `migrate_node_operator_directly`, consistent with every other state-mutating endpoint in the file:

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

If these endpoints are intentionally callable by a specific non-governance principal (e.g., the migration orchestrator), the appropriate existing helper (`check_caller_is_canister_migration_orchestrator_and_log`) should be used instead.

---

### Proof of Concept

1. Obtain any valid `SubnetId` and `NodeId` from the public registry.
2. Construct a `SwapNodeInSubnetDirectlyPayload` with those values.
3. Submit an ingress update call to the Registry canister (`rwlgt-iiaaa-aaaaa-aaaaa-cai`) method `swap_node_in_subnet_directly` from any principal (including anonymous).
4. The call succeeds, the registry mutation is applied, and `recertify_registry()` certifies the tampered state — all without any NNS governance proposal. [5](#0-4)

### Citations

**File:** rs/registry/canister/canister/canister.rs (L137-164)
```rust
fn check_caller_is_governance_or_engine_controller_and_log(method_name: &str) {
    let caller = dfn_core::api::caller();
    println!("{LOG_PREFIX}call: {method_name} from: {caller}");
    assert!(
        caller == GOVERNANCE_CANISTER_ID.into() || caller == ENGINE_CONTROLLER_CANISTER_ID.into(),
        "{LOG_PREFIX}Principal: {caller} is not authorized to call this method: {method_name}"
    );
}

fn check_caller_is_canister_migration_orchestrator_and_log(method_name: &str) {
    let caller = dfn_core::api::caller();
    println!("{LOG_PREFIX}call: {method_name} from: {caller}");
    assert_eq!(
        caller,
        MIGRATION_CANISTER_ID.into(),
        "{LOG_PREFIX}Principal: {caller} is not authorized to call this method: {method_name}"
    );
}

fn check_caller_is_subnet_rental_canister_and_log(method_name: &str) {
    let caller = dfn_core::api::caller();
    println!("{LOG_PREFIX}call: {method_name} from: {caller}");
    assert_eq!(
        caller,
        SUBNET_RENTAL_CANISTER_ID.into(),
        "{LOG_PREFIX}Principal: {caller} is not authorized to call this method: {method_name}"
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

**File:** rs/registry/canister/canister/canister.rs (L857-884)
```rust
#[unsafe(export_name = "canister_update remove_node_operators")]
fn remove_node_operators() {
    check_caller_is_governance_and_log("remove_node_operators");
    over(candid_one, |payload: RemoveNodeOperatorsPayload| {
        remove_node_operators_(payload)
    });
}

#[candid_method(update, rename = "remove_node_operators")]
fn remove_node_operators_(payload: RemoveNodeOperatorsPayload) {
    registry_mut().do_remove_node_operators(payload);
    recertify_registry();
}

#[unsafe(export_name = "canister_update update_subnet")]
fn update_subnet() {
    check_caller_is_governance_or_engine_controller_and_log("update_subnet");
    over(candid_one, |payload: UpdateSubnetPayload| {
        update_subnet_(payload)
    });
}

#[candid_method(update, rename = "update_subnet")]
fn update_subnet_(payload: UpdateSubnetPayload) {
    let caller = dfn_core::api::caller();
    registry_mut().do_update_subnet(caller, payload);
    recertify_registry();
}
```

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs (L58-65)
```rust
        // 2. Make sure that the caller is authorized to make the requested changes to node_operator_record.
        if caller
            != PrincipalId::try_from(&node_operator_record.node_provider_principal_id).unwrap()
        {
            return Err(format!(
                "Caller {caller} not equal to the node_provider_princpal_id for this record."
            ));
        }
```
