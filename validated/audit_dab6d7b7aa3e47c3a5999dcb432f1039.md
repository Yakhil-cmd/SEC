### Title
Single Subnet Admin Can Unilaterally Stop, Delete, or Uninstall Any Canister on a Rented Subnet Without Consent of Other Admins - (`rs/execution_environment/src/canister_manager.rs`, `rs/execution_environment/src/execution/common.rs`)

---

### Summary

The Internet Computer's rented-subnet feature allows up to 10 subnet admins to be registered for a subnet. Any single admin in that list can unilaterally invoke destructive management canister operations — `stop_canister`, `delete_canister`, `uninstall_code`, `canister_status`, `start_canister`, and `canister_metrics` — against **any canister on the subnet**, without the consent of the other admins. This is the direct IC analog of the multi-admin concurrent-behavior vulnerability described in the external report.

---

### Finding Description

On rented (and CloudEngine) subnets, the registry stores a list of up to `MAX_SUBNET_ADMINS` (10) principals in `SubnetRecord.subnet_admins`. Each of these principals is individually granted the same elevated privileges as a canister's own controller for a specific set of management canister (`ic00`) methods.

The authorization check in `validate_controller_or_subnet_admin` in `rs/execution_environment/src/execution/common.rs` passes if the sender is **any one** of the subnet admins:

```rust
pub(crate) fn validate_controller_or_subnet_admin(
    canister: &CanisterState,
    subnet_admins: Option<BTreeSet<PrincipalId>>,
    sender: &PrincipalId,
) -> Result<(), CanisterManagerError> {
    if canister.controllers().contains(sender) {
        Ok(())
    } else if let Some(subnet_admins) = subnet_admins {
        if subnet_admins.contains(sender) {
            Ok(())  // ← any single admin passes
        } else { ... }
    }
}
```

This check gates the following ingress-reachable methods in `rs/execution_environment/src/canister_manager.rs`:

- `CanisterStatus` / `StartCanister` / `StopCanister` / `DeleteCanister` / `UninstallCode` / `CanisterMetrics`

Additionally, `CreateCanister` via ingress is gated solely by `validate_subnet_admin`, which also passes for any single admin.

There is **no quorum, no multi-sig, and no minimum-admin-count requirement** for any of these operations. A single compromised or malicious admin can:

1. Stop and delete any canister on the subnet (destroying its state and cycles).
2. Uninstall the code of any canister (wiping its Wasm and heap, rejecting all pending calls).
3. Create new canisters (consuming subnet capacity).

The `do_update_subnet_admins` function in `rs/registry/canister/src/mutations/do_update_subnet_admins.rs` also allows `OperationType::Clear` — a single call that removes **all** admins — but this path is gated by the subnet rental canister, not by the admins themselves. However, the destructive canister operations above are directly reachable by any one admin via ingress.

---

### Impact Explanation

On a rented subnet with multiple tenant organizations each holding one admin key:

- **Canister destruction**: One admin can stop and delete any canister owned by another tenant, permanently destroying its state and discarding its cycles.
- **Code wipe**: One admin can call `uninstall_code` on any canister, wiping its Wasm and heap and rejecting all in-flight calls, without the canister's controller or other admins consenting.
- **Availability disruption**: One admin can stop all canisters on the subnet, halting all services.

The impact is **loss of availability and data integrity** for all canisters on the rented subnet, reachable by any one of up to 10 principals without coordination.

---

### Likelihood Explanation

Rented subnets are a production feature with multiple subnet admins explicitly supported (up to 10). The subnet rental canister (`SUBNET_RENTAL_CANISTER_ID`) is the authorized caller for `update_subnet_admins`. Once multiple admins are registered, any one of them can send an ingress message directly to `ic00` targeting any canister on the subnet. No special tooling is required — standard `dfx` or any IC agent suffices. The likelihood is **medium**: it requires a compromised or malicious admin key, but the attack surface is real and the impact is severe once triggered.

---

### Recommendation

1. **Require quorum for destructive operations**: Introduce a multi-sig or proposal-based mechanism so that destructive subnet-admin actions (stop, delete, uninstall) require approval from a threshold of admins rather than any single one.
2. **Scope subnet-admin power**: Consider restricting subnet admins to non-destructive operations (e.g., `canister_status`, `canister_metrics`) and requiring the canister's own controller for destructive operations (`stop`, `delete`, `uninstall_code`).
3. **Minimum admin count**: Mirror the "always at least 1 admin" invariant already present in `do_update_subnet_admins` with a "no single admin can act destructively without quorum" invariant.

---

### Proof of Concept

**Setup**: A rented subnet with two admins, `admin_A` and `admin_B`, and a canister `C` owned by `admin_B`.

**Attack path**:

1. `admin_A` sends an ingress message to `ic00` with method `stop_canister` and payload `{ canister_id: C }`.
2. `validate_controller_or_subnet_admin` is called in `rs/execution_environment/src/execution/common.rs` line 389–418. Since `admin_A` is in `subnet_admins`, it returns `Ok(())`.
3. Canister `C` is stopped.
4. `admin_A` sends `delete_canister` for `C`. `validate_controller_or_subnet_admin` again passes. `delete_canister` in `rs/execution_environment/src/canister_manager.rs` line 1270 calls `validate_controller_or_subnet_admin(canister_to_delete, subnet_admins, &sender)?` — passes. Canister `C` is permanently deleted, its state and cycles are gone.

`admin_B` (the actual owner/controller) had no ability to prevent this. No quorum was required. The test `subnet_admin_can_perform_actions_on_canister` in `rs/execution_environment/src/canister_manager/tests.rs` line 8646–8671 explicitly confirms this behavior is intentional and functional. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** rs/execution_environment/src/execution/common.rs (L389-418)
```rust
pub(crate) fn validate_controller_or_subnet_admin(
    canister: &CanisterState,
    subnet_admins: Option<BTreeSet<PrincipalId>>,
    sender: &PrincipalId,
) -> Result<(), CanisterManagerError> {
    if canister.controllers().contains(sender) {
        Ok(())
    } else if let Some(subnet_admins) = subnet_admins {
        if subnet_admins.contains(sender) {
            Ok(())
        } else {
            Err(
                CanisterManagerError::CanisterInvalidControllerOrSubnetAdmin {
                    canister_id: canister.canister_id(),
                    controllers_expected: canister.system_state.controllers.clone(),
                    subnet_admins_expected: subnet_admins,
                    caller: *sender,
                },
            )
        }
    } else {
        // If subnet admins are not set, return the same error as
        // the legacy `validate_controller` would to maintain backward compatibility.
        Err(CanisterManagerError::CanisterInvalidController {
            canister_id: canister.canister_id(),
            controllers_expected: canister.system_state.controllers.clone(),
            controller_provided: *sender,
        })
    }
}
```

**File:** rs/execution_environment/src/canister_manager.rs (L183-206)
```rust
            // These methods are only valid if they are sent by the controller
            // of the canister or a subnet admin. We assume that the canister
            // always wants to accept such messages.
            Ok(Ic00Method::CanisterStatus)
            | Ok(Ic00Method::StartCanister)
            | Ok(Ic00Method::UninstallCode)
            | Ok(Ic00Method::StopCanister)
            | Ok(Ic00Method::DeleteCanister)
            | Ok(Ic00Method::CanisterMetrics) => {
                match effective_canister_id {
                    Some(canister_id) => {
                        let canister = state.canister_state(&canister_id).ok_or_else(|| UserError::new(
                            ErrorCode::CanisterNotFound,
                            format!("Canister {canister_id} not found"),
                        ))?;
                        let subnet_admins = state.get_own_subnet_admins();
                        validate_controller_or_subnet_admin(canister, subnet_admins, sender.get_ref()).map_err(|err| err.into())
                    },
                    None => Err(UserError::new(
                        ErrorCode::InvalidManagementPayload,
                        format!("Failed to decode payload for ic00 method: {method_name}"),
                    )),
                }
            },
```

**File:** rs/execution_environment/src/canister_manager.rs (L931-948)
```rust
    ///
    /// See https://internetcomputer.org/docs/current/references/ic-interface-spec#ic-uninstall_code
    pub(crate) fn uninstall_code(
        &self,
        origin: CanisterChangeOrigin,
        canister: &mut CanisterState,
        round_limits: &mut RoundLimits,
        subnet_admins: Option<BTreeSet<PrincipalId>>,
        time: Time,
    ) -> Result<CanisterManagerResponse, CanisterManagerError> {
        let sender = origin.origin();

        // Skip the controller or subnet admins validation if the sender is the
        // governance canister. The governance canister can forcefully
        // uninstall the code of any canister.
        if sender != GOVERNANCE_CANISTER_ID.get() {
            validate_controller_or_subnet_admin(canister, subnet_admins, &sender)?;
        }
```

**File:** rs/execution_environment/src/canister_manager.rs (L1251-1270)
```rust
    pub(crate) fn delete_canister(
        &self,
        sender: PrincipalId,
        canister_id_to_delete: CanisterId,
        state: &mut ReplicatedState,
        round_limits: &mut RoundLimits,
        subnet_admins: Option<BTreeSet<PrincipalId>>,
    ) -> Result<(), CanisterManagerError> {
        let cost_schedule = state.get_own_cost_schedule();

        if let Ok(canister_id) = CanisterId::try_from(sender)
            && canister_id == canister_id_to_delete
        {
            // A canister cannot delete itself.
            return Err(CanisterManagerError::DeleteCanisterSelf(canister_id));
        }

        let canister_to_delete = self.validate_canister_exists(state, canister_id_to_delete)?;

        validate_controller_or_subnet_admin(canister_to_delete, subnet_admins, &sender)?;
```

**File:** rs/registry/canister/src/mutations/do_update_subnet_admins.rs (L157-217)
```rust
    fn compute_new_subnet_admins(
        &self,
        current_subnet_admins: Vec<PrincipalIdPb>,
        operation_type: OperationType,
    ) -> Result<Vec<PrincipalIdPb>, UpdateSubnetAdminsError> {
        let deduped_current_subnet_admins = current_subnet_admins
            .into_iter()
            .collect::<HashSet<PrincipalIdPb>>();

        let new_subnet_admins = match operation_type {
            OperationType::Add(principal_ids) => {
                if principal_ids.is_empty() {
                    return Err(UpdateSubnetAdminsError::PrincipalListEmpty);
                }

                if deduped_current_subnet_admins.len() + principal_ids.len() > MAX_SUBNET_ADMINS {
                    return Err(UpdateSubnetAdminsError::TooManySubnetAdmins {
                        provided: principal_ids.len() as u64,
                        existing: deduped_current_subnet_admins.len() as u64,
                        max_allowed: MAX_SUBNET_ADMINS as u64,
                    });
                }

                let deduped_provided_principal_ids = principal_ids
                    .into_iter()
                    .map(PrincipalIdPb::from)
                    .collect::<HashSet<PrincipalIdPb>>();

                deduped_current_subnet_admins
                    .union(&deduped_provided_principal_ids)
                    .cloned()
                    .collect()
            }
            OperationType::Remove(principal_ids) => {
                if principal_ids.is_empty() {
                    return Err(UpdateSubnetAdminsError::PrincipalListEmpty);
                }

                if principal_ids.len() > MAX_SUBNET_ADMINS {
                    return Err(UpdateSubnetAdminsError::TooManySubnetAdmins {
                        provided: principal_ids.len() as u64,
                        existing: deduped_current_subnet_admins.len() as u64,
                        max_allowed: MAX_SUBNET_ADMINS as u64,
                    });
                }

                let deduped_provided_principal_ids = principal_ids
                    .into_iter()
                    .map(PrincipalIdPb::from)
                    .collect::<HashSet<PrincipalIdPb>>();

                deduped_current_subnet_admins
                    .difference(&deduped_provided_principal_ids)
                    .cloned()
                    .collect()
            }
            OperationType::Clear(_) => vec![],
        };

        Ok(new_subnet_admins)
    }
```

**File:** rs/registry/canister/src/invariants/subnet.rs (L223-262)
```rust
// Checks that only rented subnets or cloud engine subnets can have admins, and
// that no subnet has more than `MAX_SUBNET_ADMINS` admins.
fn check_subnet_admins_invariant(
    subnet_record: &SubnetRecord,
    subnet_id: SubnetId,
) -> Result<(), InvariantCheckError> {
    // Here, it is taken that rented subnets are of type application and on a
    // free schedule. This is not very reliable and could be improved in the
    // future (e.g. by adding a new subnet type).
    let is_application_subnet = subnet_record.subnet_type == i32::from(SubnetType::Application);
    let is_on_free_cost_schedule =
        subnet_record.canister_cycles_cost_schedule == i32::from(CanisterCyclesCostSchedule::Free);
    let is_rented = is_on_free_cost_schedule && is_application_subnet;

    let is_cloud_engine_subnet =
        subnet_record.subnet_type == i32::from(SubnetType::CloudEngine) && is_on_free_cost_schedule;

    let can_have_admins =
        subnet_record.subnet_admins.is_empty() || is_rented || is_cloud_engine_subnet;
    if !can_have_admins {
        return Err(InvariantCheckError {
            msg: format!(
                "Subnet {subnet_id:} is not a rented or cloud engine subnet but has a non-empty subnet admins list"
            ),
            source: None,
        });
    }

    if subnet_record.subnet_admins.len() > MAX_SUBNET_ADMINS {
        return Err(InvariantCheckError {
            msg: format!(
                "Subnet {subnet_id:} has {} subnet admins, which exceeds the maximum of {MAX_SUBNET_ADMINS}",
                subnet_record.subnet_admins.len()
            ),
            source: None,
        });
    }

    Ok(())
}
```

**File:** rs/execution_environment/src/canister_manager/tests.rs (L8646-8671)
```rust
#[test]
fn subnet_admin_can_perform_actions_on_canister() {
    let subnet_admin = user_test_id(42);
    let mut test = ExecutionTestBuilder::new()
        .with_cost_schedule(CanisterCyclesCostSchedule::Free)
        .with_subnet_admins(vec![subnet_admin.get()])
        .build();

    let canister_id = test.universal_canister().unwrap();

    // Switch user id so the request comes from the subnet admin
    // who should not be a controller.
    test.set_user_id(subnet_admin);
    assert!(
        !test
            .canister_state(canister_id)
            .controllers()
            .contains(subnet_admin.get_ref())
    );

    assert_eq!(
        test.canister_state(canister_id).status(),
        CanisterStatusType::Running
    );

    assert_subnet_admin_actions_can_be_performed(test, canister_id);
```
