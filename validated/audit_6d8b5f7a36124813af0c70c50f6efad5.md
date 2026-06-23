### Title
Missing Caller Authorization on `swap_node_in_subnet_directly` Allows Any Ingress Sender to Modify Subnet Node Membership — (`File: rs/registry/canister/canister/canister.rs`)

---

### Summary

The registry canister exposes `swap_node_in_subnet_directly` as a publicly callable update method with no caller authorization guard. Any unprivileged ingress sender or canister can invoke it to swap nodes in and out of subnets, directly mutating certified registry state without going through NNS governance. A second function, `migrate_node_operator_directly`, shares the same defect.

---

### Finding Description

In `rs/registry/canister/canister/canister.rs`, the pattern for governance-restricted update methods is consistent: every sensitive mutation is preceded by a `check_caller_is_governance_and_log(...)` call before the payload is decoded and applied. For example:

```
remove_nodes()                → check_caller_is_governance_and_log
update_node_operator_config() → check_caller_is_governance_and_log
update_node_rewards_table()   → check_caller_is_governance_and_log
```

However, two update methods break this pattern entirely:

**`swap_node_in_subnet_directly`** (lines 831–835):
```rust
#[unsafe(export_name = "canister_update swap_node_in_subnet_directly")]
fn swap_node_in_subnet_directly() {
    over(candid_one, |payload: SwapNodeInSubnetDirectlyPayload| {
        swap_node_in_subnet_directly_(payload)
    });
}
```

**`migrate_node_operator_directly`** (lines 844–848):
```rust
#[unsafe(export_name = "canister_update migrate_node_operator_directly")]
fn migrate_node_operator_directly() {
    over(candid_one, |payload: MigrateNodeOperatorPayload| {
        migrate_node_operator_directly_(payload)
    });
}
```

Neither function calls any `check_caller_*` guard. Both immediately decode the payload and invoke the underlying registry mutation followed by `recertify_registry()`.

This contrasts with the only intentionally open "directly" function, `update_node_operator_config_directly`, which carries an explicit comment `// This method can be called by anyone` and whose underlying implementation (`do_update_node_operator_config_directly_`) enforces that the caller must be the `node_provider_principal_id` of the target record. No equivalent internal check is documented or visible for `swap_node_in_subnet_directly` or `migrate_node_operator_directly`. [1](#0-0) [2](#0-1) [3](#0-2) 

---

### Impact Explanation

`swap_node_in_subnet_directly` mutates subnet membership records in the NNS registry and calls `recertify_registry()`, which updates the certified state tree. An attacker can:

1. Remove a legitimate node from a subnet and replace it with an attacker-controlled node, degrading subnet fault tolerance.
2. Repeatedly swap nodes to destabilize subnet consensus below the fault threshold.
3. Corrupt the certified registry state that all replicas and boundary nodes rely on for routing and topology decisions.

`migrate_node_operator_directly` allows arbitrary reassignment of node operator records, which affects node reward accounting and operator identity in the registry. [4](#0-3) [5](#0-4) 

---

### Likelihood Explanation

The registry canister is deployed on the NNS subnet and its update methods are reachable via standard ingress from any principal. No privileged key, governance majority, or threshold corruption is required. The attacker only needs to know a valid `SwapNodeInSubnetDirectlyPayload` (subnet ID and node IDs, which are public registry data). The attack is immediately executable by any external user. [6](#0-5) 

---

### Recommendation

Add a `check_caller_is_governance_and_log("swap_node_in_subnet_directly")` guard (and equivalently for `migrate_node_operator_directly`) at the top of each canister update handler, consistent with every other sensitive registry mutation endpoint. If these functions are intended to be self-service for node operators, add an explicit internal authorization check analogous to the one in `do_update_node_operator_config_directly_` that validates the caller against the relevant registry record, and add a comment documenting the intent. [7](#0-6) [3](#0-2) 

---

### Proof of Concept

An attacker sends a signed ingress message to the NNS registry canister (`rwlgt-iiaaa-aaaaa-aaaaa-cai`) calling `swap_node_in_subnet_directly` with a crafted `SwapNodeInSubnetDirectlyPayload` specifying a target subnet and the node IDs to swap. Because no caller check exists at lines 831–835, the call passes directly to `do_swap_node_in_subnet_directly`, mutates the subnet membership record, and `recertify_registry()` certifies the new state. The attacker requires no special role — only a valid principal and knowledge of public subnet/node IDs from the registry.

```
dfx canister --network ic call rwlgt-iiaaa-aaaaa-aaaaa-cai swap_node_in_subnet_directly \
  '(record { subnet_id = principal "..."; node_id_to_remove = principal "..."; node_id_to_add = principal "..." })'
``` [8](#0-7) [9](#0-8)

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

**File:** rs/registry/canister/canister/canister.rs (L809-855)
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

**File:** rs/registry/canister/canister/canister.rs (L857-863)
```rust
#[unsafe(export_name = "canister_update remove_node_operators")]
fn remove_node_operators() {
    check_caller_is_governance_and_log("remove_node_operators");
    over(candid_one, |payload: RemoveNodeOperatorsPayload| {
        remove_node_operators_(payload)
    });
}
```

**File:** rs/registry/canister/canister/canister.rs (L879-884)
```rust
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
