### Title
SNS `ManagePrincipals` Permission Allows Unrestricted Self-Escalation to Full Neuron Control — (`File: rs/sns/governance/src/governance.rs`)

### Summary

In SNS Governance, a principal holding only the `ManagePrincipals` neuron permission can call `add_neuron_permissions` to grant themselves any permission listed in `neuron_grantable_permissions` — including `Disburse` — without any check preventing self-escalation beyond the caller's current permission set. This is the direct IC analog to the NEAR "Full Access Key" privilege escalation: just as a NEAR contract owner can attach a Full Access Key to gain more privileges than intended, an SNS neuron holder with `ManagePrincipals` can use it as a master key to acquire all other permissions the SNS governance allows to be granted.

---

### Finding Description

`add_neuron_permissions` in `rs/sns/governance/src/governance.rs` enforces two guards before granting permissions:

1. `check_principal_authorized_to_change_permissions` — passes if the caller holds `ManagePrincipals`.
2. `check_permissions_are_grantable` — passes if the requested permissions are a subset of `neuron_grantable_permissions`. [1](#0-0) 

Neither guard checks whether the `principal_id` receiving the new permissions is the caller themselves, nor whether the permissions being added exceed what the caller currently holds. The `principal_id` field is fully attacker-controlled. [2](#0-1) 

`check_principal_authorized_to_change_permissions` only verifies the caller has `ManagePrincipals`; it does not enforce that the caller can only grant a subset of their own permissions: [3](#0-2) 

`check_permissions_are_grantable` only checks against the SNS-wide `neuron_grantable_permissions` parameter: [4](#0-3) 

The standard SNS launch path (`SnsInitPayload::get_nervous_system_parameters`) sets `neuron_grantable_permissions` to **all permissions**: [5](#0-4) 

The default `neuron_claimer_permissions` grants only `ManagePrincipals`, `Vote`, and `SubmitProposal`: [6](#0-5) 

This creates a gap: a neuron claimer starts with `{ManagePrincipals, Vote, SubmitProposal}` but can immediately self-escalate to the full permission set including `Disburse`, `Split`, `DisburseMaturity`, `StakeMaturity`, `MergeMaturity`, and `ConfigureDissolveState`.

The integration test `test_neuron_add_all_permissions_to_self` explicitly confirms this behavior is reachable: [7](#0-6) 

---

### Impact Explanation

The `Disburse` permission allows a principal to transfer the neuron's entire staked token balance to an arbitrary ledger account. A neuron claimer who was granted only `ManagePrincipals` (and not `Disburse`) can:

1. Self-grant `Disburse` via `AddNeuronPermissions`.
2. Immediately disburse the neuron's staked tokens to any account they control.

In SNS deployments where `neuron_claimer_permissions` is intentionally restricted (e.g., to enforce a vesting schedule or limit immediate disbursement), the restriction is completely bypassed because `ManagePrincipals` acts as a master key over all permissions in `neuron_grantable_permissions`. The SNS deployer's intent to separate "can manage principals" from "can disburse funds" is not enforced by the protocol.

Additionally, `ManagePrincipals` can be used to grant `SubmitProposal` to arbitrary third-party principals, enabling unauthorized proposal submission on behalf of the neuron (which carries a `reject_cost_e8s` stake penalty).

---

### Likelihood Explanation

**Medium.** The attack requires:
- An SNS where `neuron_grantable_permissions` includes `Disburse` (true for all SNS launched via the standard `SnsInitPayload` path, which sets `neuron_grantable_permissions = all_permissions`).
- A neuron claimer whose `neuron_claimer_permissions` does not already include `Disburse` (possible when an SNS deployer customizes `neuron_claimer_permissions` to be more restrictive than `neuron_grantable_permissions`).

The attacker entry path requires no privileged access — only the ability to stake tokens and claim a neuron, which is a standard, permissionless user action on any SNS.

---

### Recommendation

Add a check in `add_neuron_permissions` that prevents a caller from granting to themselves (i.e., when `principal_id == caller`) any permission they do not already hold. Alternatively, enforce that the set of permissions a `ManagePrincipals` holder can grant is bounded by their own current permission set (a "you cannot grant what you don't have" invariant). This mirrors the standard capability-based security principle and closes the self-escalation path. [8](#0-7) 

---

### Proof of Concept

**Setup:** SNS deployed with:
- `neuron_claimer_permissions = {ManagePrincipals, Vote, SubmitProposal}` (default)
- `neuron_grantable_permissions = all` (standard SNS launch via `SnsInitPayload`)

**Steps:**

1. Attacker stakes tokens and calls `ClaimOrRefresh` to claim a neuron. Attacker receives `{ManagePrincipals, Vote, SubmitProposal}`.
2. Attacker sends an ingress `manage_neuron` update call to SNS Governance:
   ```
   ManageNeuron {
     subaccount: <attacker_neuron_subaccount>,
     command: AddNeuronPermissions({
       principal_id: <attacker_principal>,
       permissions_to_add: [Disburse, Split, DisburseMaturity, StakeMaturity, MergeMaturity, ConfigureDissolveState]
     })
   }
   ```
3. `check_principal_authorized_to_change_permissions` passes — attacker has `ManagePrincipals`.
4. `check_permissions_are_grantable` passes — all requested permissions are in `neuron_grantable_permissions`.
5. Attacker now holds all permissions. Attacker calls `Disburse` to transfer the neuron's staked tokens to an arbitrary account. [9](#0-8) [10](#0-9) [4](#0-3)

### Citations

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

**File:** rs/sns/governance/src/neuron.rs (L144-178)
```rust
    pub(crate) fn check_principal_authorized_to_change_permissions(
        &self,
        caller: &PrincipalId,
        permissions_to_change: NeuronPermissionList,
    ) -> Result<(), GovernanceError> {
        // If the permissions to change are exclusively voting related,
        // ManagePrincipals or ManageVotingPermission is sufficient.
        // Otherwise, only ManagePrincipals is sufficient.
        let sufficient_permissions = if permissions_to_change.is_exclusively_voting_related() {
            vec![
                NeuronPermissionType::ManagePrincipals,
                NeuronPermissionType::ManageVotingPermission,
            ]
        } else {
            vec![NeuronPermissionType::ManagePrincipals]
        };

        // The caller is authorized if they have any of the sufficient permissions
        let caller_authorized = sufficient_permissions
            .iter()
            .any(|sufficient_permission| self.is_authorized(caller, *sufficient_permission));

        if caller_authorized {
            Ok(())
        } else {
            let caller_permissions = self.permissions_for_principal(caller);
            Err(GovernanceError::new_with_message(
                ErrorType::NotAuthorized,
                format!(
                    "Caller '{caller:?}' is not authorized to modify permissions {permissions_to_change} for neuron '{}' as it does not have any of {sufficient_permissions:?}. (Caller's permissions are {caller_permissions})",
                    self.id.as_ref().expect("Neuron must have a NeuronId"),
                ),
            ))
        }
    }
```

**File:** rs/sns/governance/src/types.rs (L943-974)
```rust
    pub fn check_permissions_are_grantable(
        &self,
        neuron_permission_list: &NeuronPermissionList,
    ) -> Result<(), GovernanceError> {
        let mut illegal_permissions = HashSet::new();

        let grantable_permissions: HashSet<&i32> = self
            .neuron_grantable_permissions
            .as_ref()
            .expect("NervousSystemParameters.neuron_grantable_permissions must be present")
            .permissions
            .iter()
            .collect();

        for permission in &neuron_permission_list.permissions {
            if !grantable_permissions.contains(&permission) {
                illegal_permissions.insert(NeuronPermissionType::try_from(*permission).ok());
            }
        }

        if !illegal_permissions.is_empty() {
            return Err(GovernanceError::new_with_message(
                ErrorType::AccessControlList,
                format!(
                    "Cannot grant permissions as one or more permissions is not \
                    allowed to be granted. Illegal Permissions: {illegal_permissions:?}"
                ),
            ));
        }

        Ok(())
    }
```

**File:** rs/sns/init/src/lib.rs (L759-814)
```rust
        let all_permissions = NeuronPermissionList {
            permissions: NeuronPermissionType::all(),
        };

        let SnsInitPayload {
            transaction_fee_e8s,
            token_name: _,
            token_symbol: _,
            proposal_reject_cost_e8s: reject_cost_e8s,
            neuron_minimum_stake_e8s,
            fallback_controller_principal_ids: _,
            logo: _,
            url: _,
            name: _,
            description: _,
            neuron_minimum_dissolve_delay_to_vote_seconds,
            reward_rate_transition_duration_seconds,
            initial_reward_rate_basis_points,
            final_reward_rate_basis_points,
            initial_token_distribution: _,
            max_dissolve_delay_seconds,
            max_neuron_age_seconds_for_age_bonus: max_neuron_age_for_age_bonus,
            max_dissolve_delay_bonus_percentage,
            max_age_bonus_percentage,
            initial_voting_period_seconds,
            wait_for_quiet_deadline_increase_seconds,
            dapp_canisters: _,
            confirmation_text: _,
            restricted_countries: _,
            min_participants: _,
            min_icp_e8s: _,
            max_icp_e8s: _,
            min_direct_participation_icp_e8s: _,
            max_direct_participation_icp_e8s: _,
            min_participant_icp_e8s: _,
            max_participant_icp_e8s: _,
            swap_start_timestamp_seconds: _,
            swap_due_timestamp_seconds: _,
            neuron_basket_construction_parameters: _,
            nns_proposal_id: _,
            token_logo: _,
            neurons_fund_participation_constraints: _,
            neurons_fund_participation: _,
            custom_proposal_criticality,
        } = self.clone();

        let voting_rewards_parameters = Some(VotingRewardsParameters {
            reward_rate_transition_duration_seconds,
            initial_reward_rate_basis_points,
            final_reward_rate_basis_points,
            ..nervous_system_parameters.voting_rewards_parameters.unwrap()
        });

        NervousSystemParameters {
            neuron_claimer_permissions: Some(all_permissions.clone()),
            neuron_grantable_permissions: Some(all_permissions),
```

**File:** rs/sns/governance/api_helpers/src/lib.rs (L15-19)
```rust
pub const DEFAULT_NEURON_CLAIMER_PERMISSIONS: &[NeuronPermissionType] = &[
    NeuronPermissionType::ManagePrincipals,
    NeuronPermissionType::Vote,
    NeuronPermissionType::SubmitProposal,
];
```

**File:** rs/sns/integration_tests/src/neuron.rs (L1821-1890)
```rust
fn test_neuron_add_all_permissions_to_self() {
    local_test_on_sns_subnet(|runtime| async move {
        let user = Sender::from_keypair(&TEST_USER1_KEYPAIR);
        let account_identifier = Account {
            owner: user.get_principal_id().0,
            subaccount: None,
        };
        let alloc = Tokens::from_tokens(1000).unwrap();

        let system_params = NervousSystemParameters {
            // Be able to grant all permissions
            neuron_grantable_permissions: Some(NeuronPermissionList {
                permissions: NeuronPermissionType::all(),
            }),
            // ManagePrincipals will be granted to the claimer automatically
            ..NervousSystemParameters::with_default_values()
        };

        let sns_init_payload = SnsTestsInitPayloadBuilder::new()
            .with_ledger_account(account_identifier, alloc)
            .with_nervous_system_parameters(system_params)
            .build();

        let sns_canisters = SnsCanisters::set_up(&runtime, sns_init_payload).await;

        let neuron_id = sns_canisters.stake_and_claim_neuron(&user, None).await;
        let neuron = sns_canisters.get_neuron(&neuron_id).await;
        let subaccount = neuron.subaccount().expect("Error creating the subaccount");

        // Assert that the default claimer permissions are as expected before adding more
        assert_eq!(neuron.permissions.len(), 1);
        assert_eq!(
            neuron.permissions[0].principal.unwrap(),
            user.get_principal_id()
        );
        assert_eq!(
            neuron.permissions[0].permission_type.len(),
            NervousSystemParameters::with_default_values()
                .neuron_claimer_permissions
                .unwrap()
                .permissions
                .len()
        );
        assert!(neuron.permissions[0].permission_type.len() != NeuronPermissionType::all().len());

        // Grant the claimer all permissions
        sns_canisters
            .add_neuron_permissions_or_panic(
                &user,
                &subaccount,
                Some(user.get_principal_id()),
                NeuronPermissionType::all(),
            )
            .await;

        let neuron = sns_canisters.get_neuron(&neuron_id).await;
        assert_eq!(neuron.permissions.len(), 1);

        let mut neuron_permission =
            get_neuron_permission_from_neuron(&neuron, &user.get_principal_id());
        // There is no guarantee to order so sort is required for comparison
        neuron_permission.permission_type.sort_unstable();
        assert_eq!(
            neuron_permission.permission_type,
            NeuronPermissionType::all()
        );

        Ok(())
    });
}
```
