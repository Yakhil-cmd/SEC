### Title
SNS Neuron Permission Transfer While Pending Maturity Disbursement Allows Old Owner to Drain Maturity from New Owner - (`rs/sns/governance/src/governance.rs`)

---

### Summary

An SNS neuron owner can initiate a `disburse_maturity` to their own account and then immediately transfer full neuron control to a new owner via `add_neuron_permissions` / `remove_neuron_permissions`. The pending `disburse_maturity_in_progress` entry — which irrevocably routes maturity to the old owner's account — is never checked during permission changes. After the 7-day delay, the governance timer mints the maturity to the old owner, leaving the new owner with a neuron stripped of its maturity.

---

### Finding Description

SNS neurons support a two-phase maturity disbursement. When `disburse_maturity` is called, the maturity is immediately deducted from the neuron and a `DisburseMaturityInProgress` record is appended to `neuron.disburse_maturity_in_progress`, locking in the destination account at that moment. [1](#0-0) 

The actual token minting only occurs after `MATURITY_DISBURSEMENT_DELAY_SECONDS` (7 days), executed by a periodic governance timer. [2](#0-1) 

The destination account stored in `DisburseMaturityInProgress.account_to_disburse_to` is fixed at initiation time and cannot be changed or cancelled by anyone — there is no cancellation endpoint in the SNS governance canister. [3](#0-2) 

Neuron control is transferred by calling `add_neuron_permissions` (to grant all permissions to a new principal) followed by `remove_neuron_permissions` (to revoke the old principal's permissions). Neither function checks for pending `disburse_maturity_in_progress` entries before completing. [4](#0-3) [5](#0-4) 

---

### Impact Explanation

The new neuron owner loses all maturity that was committed to pending disbursements before the permission transfer. Since maturity is already deducted from `neuron.maturity_e8s_equivalent` at initiation time, the new owner observes a neuron with zero (or reduced) maturity and has no mechanism to redirect or cancel the in-flight disbursement. After 7 days the governance timer mints the full maturity amount — subject to maturity modulation — directly to the old owner's account. This is a direct loss of SNS governance tokens for the new owner.

---

### Likelihood Explanation

SNS neurons are valuable assets that are traded OTC (over-the-counter) between principals. The transfer mechanism via `add_neuron_permissions` / `remove_neuron_permissions` is the standard way to hand off neuron control. A malicious seller can execute the attack atomically: submit `disburse_maturity` in one message and `add_neuron_permissions` + `remove_neuron_permissions` in subsequent messages within the same or immediately following rounds, before the buyer can query the neuron state. Even if the buyer queries the neuron first, the seller can race the disbursement initiation against the buyer's observation window. The attack requires no privileged access — only the `DisburseMaturity` and `ManagePrincipals` permissions that any neuron owner holds by default. [6](#0-5) 

---

### Recommendation

In `add_neuron_permissions` and `remove_neuron_permissions`, before completing a permission change that would remove the last permission of the current controlling principal, check whether `neuron.disburse_maturity_in_progress` is non-empty and reject the operation (or require explicit acknowledgement). Alternatively, expose a `cancel_disburse_maturity` endpoint callable by any principal with `ManagePrincipals` permission, so a new owner can cancel pending disbursements initiated by the previous owner. [5](#0-4) 

---

### Proof of Concept

1. **Old owner** (principal `A`) holds an SNS neuron with `maturity_e8s_equivalent = 10_000_000_000` and holds `DisburseMaturity` + `ManagePrincipals` permissions.

2. **Old owner** calls `manage_neuron` with `DisburseMaturity { percentage_to_disburse: 100, to_account: Some(A's account) }`.
   - `neuron.maturity_e8s_equivalent` becomes `0`.
   - `neuron.disburse_maturity_in_progress` gains one entry: `{ amount_e8s: 10_000_000_000, account_to_disburse_to: A, finalize_at: now + 7 days }`. [7](#0-6) 

3. **Old owner** calls `manage_neuron` with `AddNeuronPermissions { principal_id: B, permissions_to_add: [all] }`.

4. **Old owner** calls `manage_neuron` with `RemoveNeuronPermissions { principal_id: A, permissions_to_remove: [all] }`.
   - Neither call checks `disburse_maturity_in_progress`. [8](#0-7) 

5. **New owner** (principal `B`) now has full control of the neuron but sees `maturity_e8s_equivalent = 0` and a `disburse_maturity_in_progress` entry pointing to `A`'s account. `B` has no way to cancel it.

6. After 7 days, the governance periodic task fires, applies maturity modulation, and mints `~10_000_000_000` SNS tokens to `A`'s account. [9](#0-8)

### Citations

**File:** rs/sns/governance/src/governance.rs (L1681-1698)
```rust
        let disbursement_in_progress = DisburseMaturityInProgress {
            amount_e8s: maturity_to_deduct,
            timestamp_of_disbursement_seconds: now_seconds,
            account_to_disburse_to: Some(to_account_proto),
            finalize_disbursement_timestamp_seconds: Some(
                now_seconds + MATURITY_DISBURSEMENT_DELAY_SECONDS,
            ),
        };

        // Re-borrow the neuron mutably to update now that the maturity has been
        // deducted and is waiting until the end of the window to modulate and disburse.
        let neuron = self.get_neuron_result_mut(id)?;
        neuron.maturity_e8s_equivalent = neuron
            .maturity_e8s_equivalent
            .saturating_sub(maturity_to_deduct);
        neuron
            .disburse_maturity_in_progress
            .push(disbursement_in_progress);
```

**File:** rs/sns/governance/src/governance.rs (L4570-4643)
```rust
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
    }
```

**File:** rs/sns/governance/src/governance.rs (L4659-4715)
```rust
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

**File:** rs/sns/governance/src/governance.rs (L4996-5009)
```rust
            let fdm = FinalizeDisburseMaturity {
                amount_to_be_disbursed_e8s: maturity_to_disburse_after_modulation_e8s,
                to_account: disbursement.account_to_disburse_to.clone(),
            };
            let in_flight_command = NeuronInFlightCommand {
                timestamp: self.env.now(),
                command: Some(neuron_in_flight_command::Command::FinalizeDisburseMaturity(
                    fdm,
                )),
            };
            let _neuron_lock = match self.lock_neuron_for_command(&neuron_id, in_flight_command) {
                Ok(neuron_lock) => neuron_lock,
                Err(_) => continue, // if locking fails, try next neuron
            };
```

**File:** rs/sns/governance/src/governance.rs (L5037-5046)
```rust
            let transfer_result = self
                .ledger
                .transfer_funds(
                    maturity_to_disburse_after_modulation_e8s,
                    0,    // Minting transfers don't pay a fee.
                    None, // This is a minting transfer, no 'from' account is needed
                    to_account,
                    self.env.now(), // The memo(nonce) for the ledger's transaction
                )
                .await;
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L12-54)
```text
enum NeuronPermissionType {
  // Unused, here for PB lint purposes.
  NEURON_PERMISSION_TYPE_UNSPECIFIED = 0;

  // The principal has permission to configure the neuron's dissolve state. This includes
  // start dissolving, stop dissolving, and increasing the dissolve delay for the neuron.
  NEURON_PERMISSION_TYPE_CONFIGURE_DISSOLVE_STATE = 1;

  // The principal has permission to add other principals to modify the neuron.
  // The nervous system parameter `NervousSystemParameters::neuron_grantable_permissions`
  // determines the maximum set of privileges that a principal can grant to another principal in
  // the given SNS.
  NEURON_PERMISSION_TYPE_MANAGE_PRINCIPALS = 2;

  // The principal has permission to submit proposals on behalf of the neuron.
  // Submitting proposals can change a neuron's stake and thus this
  // is potentially a balance changing operation.
  NEURON_PERMISSION_TYPE_SUBMIT_PROPOSAL = 3;

  // The principal has permission to vote and follow other neurons on behalf of the neuron.
  NEURON_PERMISSION_TYPE_VOTE = 4;

  // The principal has permission to disburse the neuron.
  NEURON_PERMISSION_TYPE_DISBURSE = 5;

  // The principal has permission to split the neuron.
  NEURON_PERMISSION_TYPE_SPLIT = 6;

  // The principal has permission to merge the neuron's maturity into
  // the neuron's stake.
  NEURON_PERMISSION_TYPE_MERGE_MATURITY = 7;

  // The principal has permission to disburse the neuron's maturity to a
  // given ledger account.
  NEURON_PERMISSION_TYPE_DISBURSE_MATURITY = 8;

  // The principal has permission to stake the neuron's maturity.
  NEURON_PERMISSION_TYPE_STAKE_MATURITY = 9;

  // The principal has permission to grant/revoke permission to vote and submit
  // proposals on behalf of the neuron to other principals.
  NEURON_PERMISSION_TYPE_MANAGE_VOTING_PERMISSION = 10;
}
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L88-95)
```text
message DisburseMaturityInProgress {
  // This field is the quantity of maturity in e8s that has been decremented from a Neuron to
  // be modulated and disbursed as SNS tokens.
  uint64 amount_e8s = 1;
  uint64 timestamp_of_disbursement_seconds = 2;
  Account account_to_disburse_to = 3;
  optional uint64 finalize_disbursement_timestamp_seconds = 4;
}
```
