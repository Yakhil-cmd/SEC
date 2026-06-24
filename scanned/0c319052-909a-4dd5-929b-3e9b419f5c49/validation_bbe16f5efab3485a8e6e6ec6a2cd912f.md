### Title
Missing Caller Authorization on `swap_node_in_subnet_directly` and `migrate_node_operator_directly` Registry Canister Update Methods - (`File: rs/registry/canister/canister/canister.rs`)

### Summary
The Registry canister exposes two privileged `canister_update` endpoints — `swap_node_in_subnet_directly` and `migrate_node_operator_directly` — without any caller authorization guard. Every other privileged mutation method in the same file calls a `check_caller_is_*` function before executing. These two methods do not, meaning any unprivileged ingress sender or canister can invoke them directly.

### Finding Description
In `rs/registry/canister/canister/canister.rs`, the pattern for privileged registry mutations is consistent: the outer `canister_update` export calls a `check_caller_is_*` helper before dispatching to the inner implementation. For example:

- `remove_api_boundary_nodes` calls `check_caller_is_governance_and_log`
- `update_node_operator_config` calls `check_caller_is_governance_and_log`
- `prepare_canister_migration` calls `check_caller_is_governance_and_log`
- `update_subnet_admins` calls `check_caller_is_subnet_rental_canister_and_log`

However, `swap_node_in_subnet_directly` (lines 831–836) and `migrate_node_operator_directly` (lines 844–855) dispatch directly to their inner implementations with no caller check at all:

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

The only intentionally open method in this file is `update_node_operator_config_directly`, which carries an explicit comment `// This method can be called by anyone` and has internal per-record authorization (caller must equal the node provider principal). Neither `swap_node_in_subnet_directly` nor `migrate_node_operator_directly` carry such a comment, and the implementation files (`do_swap_node_in_subnet_directly.rs`, `do_migrate_node_operator_directly.rs`) were not confirmed to contain equivalent internal authorization logic.

### Impact Explanation
The Registry canister is the authoritative source of truth for the IC network topology. Unauthorized writes to it can:
- Swap nodes between subnets arbitrarily, disrupting subnet membership and consensus
- Migrate node operator records without the node provider's consent, corrupting operator-to-node mappings

These are governance-level operations. Allowing any ingress sender to invoke them bypasses the NNS proposal process entirely, enabling a single unprivileged principal to alter the live network topology.

### Likelihood Explanation
The Registry canister is publicly reachable via ingress. Any principal can submit an update call to `swap_node_in_subnet_directly` or `migrate_node_operator_directly` with a crafted payload. No special privilege, key, or threshold corruption is required. The attack path is a direct ingress message.

### Recommendation
Add the appropriate caller check to both methods, consistent with the rest of the file. For governance-gated operations:

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
    check_caller_is_canister_migration_orchestrator_and_log("migrate_node_operator_directly");
    over(candid_one, |payload: MigrateNodeOperatorPayload| {
        migrate_node_operator_directly_(payload)
    });
}
```

If either method is intentionally callable by node operators directly (like `update_node_operator_config_directly`), an explicit comment and internal per-record authorization check must be added and verified.

### Proof of Concept
The attacker-controlled entry path is a direct ingress update call:

1. Attacker observes two valid `node_id` values from the public registry state.
2. Attacker submits an ingress `update` call to the Registry canister targeting `swap_node_in_subnet_directly` with a `SwapNodeInSubnetDirectlyPayload` containing those node IDs.
3. Because no `check_caller_is_*` guard exists, the call proceeds to `do_swap_node_in_subnet_directly`, mutates the registry, and `recertify_registry()` is called — committing the unauthorized topology change to certified state.

The analogous Solidity pattern from the report is exact: `payableCall()` lacked `requiresApprovedCaller` while `call()` had it. Here, `swap_node_in_subnet_directly` and `migrate_node_operator_directly` lack `check_caller_is_*` while every neighboring method has it. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** rs/registry/canister/canister/canister.rs (L127-164)
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

**File:** rs/registry/canister/canister/canister.rs (L809-830)
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

**File:** rs/registry/canister/canister/canister.rs (L857-860)
```rust
#[unsafe(export_name = "canister_update remove_node_operators")]
fn remove_node_operators() {
    check_caller_is_governance_and_log("remove_node_operators");
    over(candid_one, |payload: RemoveNodeOperatorsPayload| {
```
