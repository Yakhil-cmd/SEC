### Title
Missing Audit Logs for SNS Neuron Permission Changes and NNS Hot Key Mutations - (File: `rs/sns/governance/src/governance.rs`, `rs/nns/governance/src/neuron/types.rs`)

---

### Summary
The SNS governance canister's `add_neuron_permissions` and `remove_neuron_permissions` functions, and the NNS governance canister's `add_hot_key` / `remove_hot_key` operations, execute access-control mutations with no structured audit log entry recording which principal was granted or revoked, which permissions changed, or on which neuron. The only log emitted is the generic `manage_neuron/{command_name}` at the dispatch layer. This is the direct IC analog of the Futureswap M06 finding: important state stored in non-enumerable per-neuron structures is mutated without any queryable event trail.

---

### Finding Description

**SNS Governance — `add_neuron_permissions` / `remove_neuron_permissions`**

`add_neuron_permissions` at `rs/sns/governance/src/governance.rs:4570–4642` and `remove_neuron_permissions` at `rs/sns/governance/src/governance.rs:4659–4715` mutate the `permissions` field of a neuron (a `Vec<NeuronPermission>`) without emitting any log entry that records the affected `neuron_id`, the `principal_id` being granted or revoked, or the specific `NeuronPermissionType` values changed. [1](#0-0) 

The only log that exists is at the dispatch layer: [2](#0-1) 

This logs `manage_neuron/add_neuron_permissions` — the command name only — with no detail about which principal or permissions were affected.

**NNS Governance — `add_hot_key` / `remove_hot_key`**

`add_hot_key` at `rs/nns/governance/src/neuron/types.rs:657–676` and `remove_hot_key` at `rs/nns/governance/src/neuron/types.rs:679–690` mutate the `hot_keys: Vec<PrincipalId>` field of a neuron with zero logging at any layer. [3](#0-2) 

The calling `configure_neuron` function also emits no log: [4](#0-3) 

Hot keys grant the ability to vote and follow on behalf of a neuron, directly affecting NNS governance outcomes.

---

### Impact Explanation

**SNS**: Any principal holding `ManagePrincipals` permission on an SNS neuron can silently grant or revoke any permission (including `ManagePrincipals` itself) to any other principal. Because `NeuronPermission` entries are stored per-neuron and there is no canister-level event log, an observer cannot reconstruct the history of who was granted or revoked access without continuously polling `get_neuron`. A compromised or malicious neuron controller can add a backdoor principal with full permissions and the action leaves no trace in the canister logs beyond the bare command name. [5](#0-4) 

**NNS**: Hot key additions are callable by any neuron controller. A neuron with large voting power that silently adds a hot key controlled by an attacker can be used to vote on NNS proposals without the neuron owner's knowledge. There is no log entry recording the new hot key principal or the neuron ID. [6](#0-5) 

---

### Likelihood Explanation

Both operations are reachable by any unprivileged ingress sender who controls a neuron (SNS) or any neuron controller (NNS). No privileged role, admin key, or governance majority is required. The entry path is a standard `manage_neuron` update call, which is the primary user-facing API for both governance systems. [7](#0-6) [8](#0-7) 

---

### Recommendation

1. In `add_neuron_permissions` and `remove_neuron_permissions`, emit a structured `log!(INFO, ...)` entry recording `neuron_id`, `principal_id`, and the permission types added/removed before returning `Ok(())`.
2. In `add_hot_key` and `remove_hot_key` (or in `configure_neuron`), emit a log entry recording the neuron ID and the hot key principal being added or removed.
3. Consider whether these events should be surfaced as certified state or ICRC-3-style transaction log entries for SNS ledger-integrated governance systems, enabling off-chain indexers to reconstruct the full permission history.

---

### Proof of Concept

1. Deploy an SNS. Obtain a neuron with `ManagePrincipals` permission.
2. Call `manage_neuron` with `AddNeuronPermissions { principal_id: attacker, permissions_to_add: [all] }`.
3. Inspect canister logs: only `manage_neuron/add_neuron_permissions` appears — no principal ID, no neuron ID, no permission list.
4. The attacker's principal now has full control of the neuron. No log entry records this. [9](#0-8) [10](#0-9) [11](#0-10)

### Citations

**File:** rs/sns/governance/src/governance.rs (L4553-4642)
```rust
    /// Adds a `NeuronPermission` to an already existing Neuron for the given PrincipalId.
    ///
    /// If the PrincipalId doesn't have existing permissions, a new entry will be added for it
    /// with the provided permissions. If a principalId already has permissions for this neuron,
    /// the new permissions will be added to the existing set.
    ///
    /// Preconditions:
    /// - the caller has the permission to change a neuron's access control
    ///   (permission `ManagePrincipals`), or the caller has the permission to
    ///   manage voting-related permissions (permission `ManageVotingPermission`)
    ///   and the permissions being added are voting-related.
    /// - the permissions provided in the request are a subset of neuron_grantable_permissions
    ///   as defined in the nervous system parameters. To see what the current parameters are
    ///   for an SNS see `get_nervous_system_parameters`.
    /// - adding the new permissions for the principal does not exceed the limit of principals
    ///   that a neuron can have in its access control list, which is defined by the nervous
    ///   system parameter max_number_of_principals_per_neuron
    fn add_neuron_permissions(
        &mut self,
        neuron_id: &NeuronId,
        caller: &PrincipalId,
        add_neuron_permissions: &AddNeuronPermissions,
    ) -> Result<(), GovernanceError> {
        let neuron = self.get_neuron_result(neuron_id)?;

        let permissions_to_add = add_neuron_permissions
            .permissions_to_add
            .as_ref()
            .ok_or_else(|| {
                GovernanceError::new_with_message(
                    ErrorType::InvalidCommand,
                    "AddNeuronPermissions command must provide permissions to add",
                )
            })?;

        // A simple check to prevent DoS attack with large number of permission changes.
        if permissions_to_add.permissions.len() > NeuronPermissionType::all().len() {
            return Err(GovernanceError::new_with_message(
                ErrorType::InvalidCommand,
                "AddNeuronPermissions command provided more permissions than exist in the system",
            ));
        }

        neuron
            .check_principal_authorized_to_change_permissions(caller, permissions_to_add.clone())?;

        self.nervous_system_parameters_or_panic()
            .check_permissions_are_grantable(permissions_to_add)?;

        let principal_id = add_neuron_permissions.principal_id.ok_or_else(|| {
            GovernanceError::new_with_message(
                ErrorType::InvalidCommand,
                "AddNeuronPermissions command must provide a PrincipalId to add permissions to",
            )
        })?;

        let existing_permissions = neuron
            .permissions
            .iter()
            .find(|permission| permission.principal == Some(principal_id));

        let max_number_of_principals_per_neuron = self
            .nervous_system_parameters_or_panic()
            .max_number_of_principals_per_neuron
            .expect("NervousSystemParameters.max_number_of_principals_per_neuron must be present");

        // If the PrincipalId does not already exist in the neuron, make sure it can be added
        if existing_permissions.is_none()
            && neuron.permissions.len() == max_number_of_principals_per_neuron as usize
        {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "Cannot add permission to neuron. Max \
                    number of principals reached {max_number_of_principals_per_neuron}"
                ),
            ));
        }

        // Re-borrow the neuron mutably to update now that the preconditions have been met
        self.get_neuron_result_mut(neuron_id)?
            .add_permissions_for_principal(principal_id, permissions_to_add.permissions.clone());

        GovernanceProto::add_neuron_to_principal_in_principal_to_neuron_ids_index(
            &mut self.principal_to_neuron_ids_index,
            neuron_id,
            &principal_id,
        );

        Ok(())
```

**File:** rs/sns/governance/src/governance.rs (L4645-4715)
```rust
    /// Removes a set of permissions for a PrincipalId on an existing Neuron.
    ///
    /// If all the permissions are removed from the Neuron i.e. by removing all permissions for
    /// all PrincipalIds, the Neuron is not deleted. This is a dangerous operation as it is
    /// possible to remove all permissions for a neuron and no longer be able to modify its
    /// state, i.e. disbursing the neuron back into the governance token.
    ///
    /// Preconditions:
    /// - the caller has the permission to change a neuron's access control
    ///   (permission `ManagePrincipals`), or the caller has the permission to
    ///   manage voting-related permissions (permission `ManageVotingPermission`)
    ///   and the permissions being removed are voting-related.
    /// - the PrincipalId exists within the neuron's permissions
    /// - the PrincipalId's NeuronPermission contains the permission_types that are to be removed
    fn remove_neuron_permissions(
        &mut self,
        neuron_id: &NeuronId,
        caller: &PrincipalId,
        remove_neuron_permissions: &RemoveNeuronPermissions,
    ) -> Result<(), GovernanceError> {
        let neuron = self.get_neuron_result(neuron_id)?;

        let permissions_to_remove = remove_neuron_permissions
            .permissions_to_remove
            .as_ref()
            .ok_or_else(|| {
                GovernanceError::new_with_message(
                    ErrorType::InvalidCommand,
                    "RemoveNeuronPermissions command must provide permissions to remove",
                )
            })?;

        // A simple check to prevent DoS attack with large number of permission changes.
        if permissions_to_remove.permissions.len() > NeuronPermissionType::all().len() {
            return Err(GovernanceError::new_with_message(
                ErrorType::InvalidCommand,
                "RemoveNeuronPermissions command provided more permissions than exist in the system",
            ));
        }

        let principal_id = remove_neuron_permissions
            .principal_id
            .ok_or_else(|| {
                GovernanceError::new_with_message(
                    ErrorType::InvalidCommand,
                    "RemoveNeuronPermissions command must provide a PrincipalId to remove permissions from",
                )
            })?;

        neuron.check_principal_authorized_to_change_permissions(
            caller,
            permissions_to_remove.clone(),
        )?;

        // Re-borrow the neuron mutably to update now that the preconditions have been met
        let principal_id_was_removed = self
            .get_neuron_result_mut(neuron_id)?
            .remove_permissions_for_principal(
                principal_id,
                permissions_to_remove.permissions.clone(),
            )?;

        if principal_id_was_removed == RemovePermissionsStatus::AllPermissionTypesRemoved {
            GovernanceProto::remove_neuron_from_principal_in_principal_to_neuron_ids_index(
                &mut self.principal_to_neuron_ids_index,
                neuron_id,
                &principal_id,
            )
        }

        Ok(())
```

**File:** rs/sns/governance/src/governance.rs (L4749-4758)
```rust
    /// Calls manage_neuron_internal and unwraps the result in a ManageNeuronResponse.
    pub async fn manage_neuron(
        &mut self,
        mgmt: &ManageNeuron,
        caller: &PrincipalId,
    ) -> ManageNeuronResponse {
        self.manage_neuron_internal(caller, mgmt)
            .await
            .unwrap_or_else(ManageNeuronResponse::error)
    }
```

**File:** rs/sns/governance/src/governance.rs (L4779-4779)
```rust
        log!(INFO, "manage_neuron/{}", command.command_name());
```

**File:** rs/nns/governance/src/neuron/types.rs (L653-690)
```rust
    /// Preconditions:
    /// - key to add is not already present in 'hot_keys'
    /// - the key to add is well-formed
    /// - there are not already too many hot keys for this neuron.
    fn add_hot_key(&mut self, new_hot_key: &PrincipalId) -> Result<(), GovernanceError> {
        // Make sure that the same hot key is not added twice.
        for key in &self.hot_keys {
            if *key == *new_hot_key {
                return Err(GovernanceError::new_with_message(
                    ErrorType::HotKey,
                    "Hot key duplicated.",
                ));
            }
        }
        // Allow at most 10 hot keys per neuron.
        if self.hot_keys.len() >= MAX_NUM_HOT_KEYS_PER_NEURON {
            return Err(GovernanceError::new_with_message(
                ErrorType::ResourceExhausted,
                "Reached the maximum number of hotkeys.",
            ));
        }
        self.hot_keys.push(*new_hot_key);
        Ok(())
    }

    /// Precondition: key to remove is present in 'hot_keys'
    fn remove_hot_key(&mut self, hot_key_to_remove: &PrincipalId) -> Result<(), GovernanceError> {
        if let Some(index) = self.hot_keys.iter().position(|x| *x == *hot_key_to_remove) {
            self.hot_keys.swap_remove(index);
            Ok(())
        } else {
            // Hot key to remove was not found.
            Err(GovernanceError::new_with_message(
                ErrorType::NotFound,
                "Remove failed: Hot key not found.",
            ))
        }
    }
```

**File:** rs/nns/governance/src/governance.rs (L5793-5810)
```rust
    fn configure_neuron(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        c: &manage_neuron::Configure,
    ) -> Result<(), GovernanceError> {
        let now_seconds = self.env.now();

        let lock_command = NeuronInFlightCommand {
            timestamp: now_seconds,
            command: Some(InFlightCommand::Configure(c.clone())),
        };
        let _lock = self.lock_neuron_for_command(id.id, lock_command)?;

        self.with_neuron_mut(id, |neuron| neuron.configure(caller, now_seconds, c))??;

        Ok(())
    }
```

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L780-788)
```text
  message AddHotKey {
    option (ic_base_types.pb.v1.tui_signed_message) = true;
    ic_base_types.pb.v1.PrincipalId new_hot_key = 1 [(ic_base_types.pb.v1.tui_signed_display_q2_2021) = true];
  }
  // Remove a hot key that has been previously assigned to the neuron.
  message RemoveHotKey {
    option (ic_base_types.pb.v1.tui_signed_message) = true;
    ic_base_types.pb.v1.PrincipalId hot_key_to_remove = 1 [(ic_base_types.pb.v1.tui_signed_display_q2_2021) = true];
  }
```
