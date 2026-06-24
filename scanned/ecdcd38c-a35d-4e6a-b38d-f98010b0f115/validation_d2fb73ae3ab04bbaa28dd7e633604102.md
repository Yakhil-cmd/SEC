### Title
Deleted Subnet Remains Valid in CMC's `default_subnets` and `subnet_types_to_subnets` After Registry Removal — (`rs/nns/cmc/src/main.rs`)

---

### Summary

`remove_subnet_from_authorized_subnet_list` in the Cycles Minting Canister (CMC) only removes a deleted subnet from `authorized_subnets`, but does **not** remove it from `default_subnets` or `subnet_types_to_subnets`. This is a direct analog of the `OperatorVCS::removeVault` bug: one data structure is updated on removal while a secondary validity-tracking structure is left stale, allowing the removed entity to continue being treated as active.

---

### Finding Description

The CMC tracks eligible subnets for canister creation in three independent data structures:

1. `state.authorized_subnets` — per-principal authorized subnet lists
2. `state.default_subnets` — global default subnet list (anyone can deploy here)
3. `state.subnet_types_to_subnets` — type-keyed subnet sets (e.g., "fiduciary")

When the Registry canister deletes a subnet, it calls `remove_subnet_from_authorized_subnet_list` on the CMC. This function only removes the subnet from `authorized_subnets`: [1](#0-0) 

It does **not** touch `default_subnets` or `subnet_types_to_subnets`. However, `do_create_canister` accepts a subnet as valid if it appears in **any** of the three structures: [2](#0-1) 

So if a subnet was registered in `default_subnets` (via `set_authorized_subnetwork_list` with `who = None`) or in `subnet_types_to_subnets` (via `change_subnet_type_assignment`), and is subsequently deleted from the registry, it remains permanently "valid" in CMC state. Any user can still target it for canister creation.

The `set_authorized_subnetwork_list` function enforces a one-way invariant — it prevents adding a subnet that is already in `subnet_types_to_subnets` — but there is no symmetric cleanup when a subnet is deleted: [3](#0-2) 

The `remove_subnet_type` function also refuses to remove a type that still has assigned subnets, meaning `subnet_types_to_subnets` entries for a deleted subnet cannot be cleaned up through the normal governance path either: [4](#0-3) 

---

### Impact Explanation

After a subnet is deleted from the registry:

- Any user calling `create_canister` or `notify_create_canister` with `SubnetSelection::Subnet { subnet: <deleted_subnet> }` will pass the CMC's authorization check (the subnet is still in `default_subnets` or `subnet_types_to_subnets`).
- The CMC will proceed to call the management canister to create a canister on the non-existent subnet.
- The management canister call will fail, but cycles have already been minted and consumed. Depending on error-handling paths, users may lose cycles.
- Additionally, `subnet_types_to_subnets` entries for deleted subnets cannot be cleaned up via `remove_subnet_type` (which requires the type's subnet set to be empty first), creating a permanent stale entry that blocks re-use of the type name.

---

### Likelihood Explanation

Subnet deletion is a rare but legitimate governance action (there is a `delete_subnet` proposal type in the registry). Application subnets are routinely added to `default_subnets` and fiduciary/typed subnets are added to `subnet_types_to_subnets`. Any such subnet that is later deleted will trigger this inconsistency. The entry path for exploitation is a normal unprivileged `create_canister` or `notify_create_canister` ingress call — no special privileges are required after the governance action has occurred.

---

### Recommendation

`remove_subnet_from_authorized_subnet_list` should also remove the subnet from `default_subnets` and from all entries in `subnet_types_to_subnets`:

```rust
fn remove_subnet_from_authorized_subnet_list(arg: RemoveSubnetFromAuthorizedSubnetListArgs) {
    // ... auth check ...
    with_state_mut(|state| {
        // existing: remove from authorized_subnets
        state.authorized_subnets
            .values_mut()
            .for_each(|list| list.retain(|s| *s != subnet_to_remove));

        // NEW: remove from default_subnets
        state.default_subnets.retain(|s| *s != subnet_to_remove);

        // NEW: remove from subnet_types_to_subnets
        if let Some(ref mut types_to_subnets) = state.subnet_types_to_subnets {
            for subnets in types_to_subnets.values_mut() {
                subnets.remove(&subnet_to_remove);
            }
        }
    });
}
```

---

### Proof of Concept

1. Via governance, call `set_authorized_subnetwork_list` with `who = None, subnets = [subnet_X]` — adds `subnet_X` to `default_subnets`.
2. Via governance, submit and execute a `DeleteSubnet` proposal for `subnet_X` — the registry deletes the subnet and calls `remove_subnet_from_authorized_subnet_list(subnet_X)` on CMC.
3. Observe: `subnet_X` is removed from `authorized_subnets` but **remains** in `default_subnets`.
4. Any user calls `create_canister` with `SubnetSelection::Subnet { subnet: subnet_X }` and sufficient ICP/cycles.
5. CMC passes the authorization check at line 2208 (`state.default_subnets.contains(&subnet)` → `true`) and proceeds to call the management canister.
6. The management canister rejects the call (subnet does not exist); cycles are consumed without a canister being created. [5](#0-4) [6](#0-5)

### Citations

**File:** rs/nns/cmc/src/main.rs (L505-522)
```rust
        let assigned_to_types: BTreeSet<&SubnetId> = state
            .subnet_types_to_subnets
            .as_ref()
            .expect("subnet types to subnets mapping is not `None`")
            .values()
            .flatten()
            .collect();
        let mut already_assigned = vec![];
        for subnet in subnets.iter() {
            if assigned_to_types.contains(subnet) {
                already_assigned.push(*subnet);
            }
        }
        if !already_assigned.is_empty() {
            panic!(
                "Subnets {already_assigned:?} are already assigned to a type and cannot be authorized."
            );
        }
```

**File:** rs/nns/cmc/src/main.rs (L591-607)
```rust
        match subnet_types_to_subnets.get(&subnet_type) {
            Some(subnets) => {
                if !subnets.is_empty() {
                    Err(UpdateSubnetTypeError::TypeHasAssignedSubnets((
                        subnet_type,
                        subnets.iter().copied().collect(),
                    )))
                } else {
                    print(format!("[cycles] Removing subnet type: {subnet_type}"));
                    // Type does not have any assigned subnets, so it can be removed.
                    subnet_types_to_subnets.remove(&subnet_type);
                    Ok(())
                }
            }
            None => Err(UpdateSubnetTypeError::TypeDoesNotExist(subnet_type)),
        }
    })
```

**File:** rs/nns/cmc/src/main.rs (L1103-1123)
```rust
#[update(hidden = true)]
fn remove_subnet_from_authorized_subnet_list(arg: RemoveSubnetFromAuthorizedSubnetListArgs) {
    let RemoveSubnetFromAuthorizedSubnetListArgs {
        subnet: subnet_to_remove,
    } = arg;
    let caller = caller();
    assert_eq!(
        caller,
        REGISTRY_CANISTER_ID.into(),
        "{} is not authorized to call this method: {}",
        caller,
        "remove_subnet_from_authorized_subnet_list"
    );

    with_state_mut(|state| {
        state
            .authorized_subnets
            .values_mut()
            .for_each(|subnet_list| subnet_list.retain(|subnet| *subnet != subnet_to_remove))
    });
}
```

**File:** rs/nns/cmc/src/main.rs (L2207-2230)
```rust
            SubnetSelection::Subnet { subnet } => with_state(|state| {
                if state.default_subnets.contains(&subnet)
                    || state
                        .authorized_subnets
                        .get(&controller_id)
                        .map(|subnets| subnets.contains(&subnet))
                        .unwrap_or(false)
                    || state
                        .subnet_types_to_subnets
                        .as_ref()
                        .map(|types_to_subnets| {
                            types_to_subnets
                                .values()
                                .any(|subnets| subnets.contains(&subnet))
                        })
                        .unwrap_or(false)
                {
                    Ok(vec![subnet])
                } else {
                    Err(format!(
                        "Subnet {subnet} does not exist or {controller_id} is not authorized to deploy to that subnet."
                    ))
                }
            }),
```
