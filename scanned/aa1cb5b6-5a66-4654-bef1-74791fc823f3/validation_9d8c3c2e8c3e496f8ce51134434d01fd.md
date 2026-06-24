### Title
SNS Neuron Holders Cannot Exit Their Position Under Default `NervousSystemParameters` Configuration - (File: `rs/sns/governance/src/types.rs`, `rs/sns/governance/src/governance.rs`)

### Summary
The SNS governance canister's default `NervousSystemParameters` configuration does not include `NeuronPermissionType::Disburse` or `NeuronPermissionType::ConfigureDissolveState` in either `neuron_claimer_permissions` or `neuron_grantable_permissions`. As a result, any user who claims an SNS neuron under the default configuration cannot dissolve or disburse their staked tokens without the cooperation of the SNS governance community passing a parameter-change proposal.

### Finding Description

The `REQUIRED_NEURON_CLAIMER_PERMISSIONS` constant and the `with_default_values()` function define the minimum set of permissions a neuron claimer receives and the set of permissions that can be granted to others: [1](#0-0) 

The required claimer permissions are only `ManagePrincipals`, `Vote`, and `SubmitProposal`. Neither `Disburse` nor `ConfigureDissolveState` is included. [2](#0-1) 

The default `neuron_grantable_permissions` is set to `NeuronPermissionList::default()`, which is an **empty list**. This means a claimer with `ManagePrincipals` cannot grant themselves or anyone else `Disburse` or `ConfigureDissolveState` because those permissions are not in the grantable set.

The `disburse_neuron` function in SNS governance enforces `NeuronPermissionType::Disburse`: [3](#0-2) 

The `configure_neuron` function enforces `NeuronPermissionType::ConfigureDissolveState`: [4](#0-3) 

The `remove_neuron_permissions` function itself acknowledges this danger in its docstring: [5](#0-4) 

The `RemoveNeuronPermissions` proto message also documents this risk: [6](#0-5) 

The `api_helpers` crate exposes the same restricted default: [7](#0-6) 

Note: The standard `SnsInitPayload::get_nervous_system_parameters()` path grants all permissions, but any SNS that uses `NervousSystemParameters::with_default_values()` directly, or any SNS governance proposal that reduces `neuron_grantable_permissions` to exclude `Disburse`/`ConfigureDissolveState`, produces this locked state for all subsequently claimed neurons.

### Impact Explanation

A user who claims an SNS neuron under the default configuration receives only `{ManagePrincipals, Vote, SubmitProposal}`. They:
- Cannot call `StartDissolving` (requires `ConfigureDissolveState`)
- Cannot call `Disburse` (requires `Disburse`)
- Cannot grant themselves these permissions (grantable set is empty)

Their staked governance tokens are permanently locked until the SNS community passes a `ManageNervousSystemParameters` proposal to expand `neuron_grantable_permissions`. This is a permanent, unrecoverable lock of user funds without any privileged attacker action — it is the default behavior of the system.

### Likelihood Explanation

Any SNS deployed using `NervousSystemParameters::with_default_values()` without explicitly setting `neuron_claimer_permissions` and `neuron_grantable_permissions` to include `Disburse` and `ConfigureDissolveState` will exhibit this behavior. Additionally, an SNS governance proposal that reduces `neuron_grantable_permissions` (e.g., to tighten access control) can retroactively trap all future neuron claimers. The entry path is a standard unprivileged ingress call to `manage_neuron` with `ClaimOrRefresh`.

### Recommendation

1. Add `NeuronPermissionType::Disburse` and `NeuronPermissionType::ConfigureDissolveState` to `REQUIRED_NEURON_CLAIMER_PERMISSIONS` so that neuron holders always have the ability to exit their position.
2. Alternatively, add a validation in `validate_neuron_claimer_permissions` that rejects configurations where `Disburse` or `ConfigureDissolveState` is absent from both `neuron_claimer_permissions` and `neuron_grantable_permissions`.
3. Add a validation in `ManageNervousSystemParameters` proposal execution that prevents reducing `neuron_grantable_permissions` below a safe floor that includes exit-related permissions.

### Proof of Concept

1. Deploy an SNS using `NervousSystemParameters::with_default_values()` (claimer permissions = `{ManagePrincipals, Vote, SubmitProposal}`, grantable = `{}`).
2. User calls `manage_neuron` with `ClaimOrRefresh` — neuron is created with only `{ManagePrincipals, Vote, SubmitProposal}`.
3. User calls `manage_neuron` with `Disburse` → `check_authorized` fails with `NotAuthorized` because `Disburse` is not in the neuron's permission list.
4. User calls `manage_neuron` with `Configure { StartDissolving }` → `check_authorized` fails with `NotAuthorized` because `ConfigureDissolveState` is absent.
5. User calls `manage_neuron` with `AddNeuronPermissions { Disburse }` → `check_permissions_are_grantable` fails because `neuron_grantable_permissions` is empty.
6. User's staked tokens are permanently locked with no on-chain exit path. [1](#0-0) [8](#0-7) [9](#0-8)

### Citations

**File:** rs/sns/governance/src/types.rs (L437-445)
```rust
    pub const REQUIRED_NEURON_CLAIMER_PERMISSIONS: &'static [NeuronPermissionType] = &[
        // Without this permission, it would be impossible to transfer control
        // of a neuron to a new principal.
        NeuronPermissionType::ManagePrincipals,
        // Without this permission, it would be impossible to vote.
        NeuronPermissionType::Vote,
        // Without this permission, it would be impossible to submit a proposal.
        NeuronPermissionType::SubmitProposal,
    ];
```

**File:** rs/sns/governance/src/types.rs (L484-486)
```rust
            neuron_claimer_permissions: Some(Self::default_neuron_claimer_permissions()),
            neuron_grantable_permissions: Some(NeuronPermissionList::default()),
            max_number_of_principals_per_neuron: Some(5),
```

**File:** rs/sns/governance/src/governance.rs (L1119-1127)
```rust
    pub async fn disburse_neuron(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        disburse: &manage_neuron::Disburse,
    ) -> Result<u64, GovernanceError> {
        // First check authorized
        let neuron = self.get_neuron_result(id)?;
        neuron.check_authorized(caller, NeuronPermissionType::Disburse)?;
```

**File:** rs/sns/governance/src/governance.rs (L4169-4183)
```rust
    fn configure_neuron(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        configure: &manage_neuron::Configure,
    ) -> Result<(), GovernanceError> {
        let now = self.env.now();

        self.proto
            .neurons
            .get(&id.to_string())
            .ok_or_else(|| Self::neuron_not_found_error(id))
            .and_then(|neuron| {
                neuron.check_authorized(caller, NeuronPermissionType::ConfigureDissolveState)
            })?;
```

**File:** rs/sns/governance/src/governance.rs (L4645-4651)
```rust
    /// Removes a set of permissions for a PrincipalId on an existing Neuron.
    ///
    /// If all the permissions are removed from the Neuron i.e. by removing all permissions for
    /// all PrincipalIds, the Neuron is not deleted. This is a dangerous operation as it is
    /// possible to remove all permissions for a neuron and no longer be able to modify its
    /// state, i.e. disbursing the neuron back into the governance token.
    ///
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L2022-2025)
```text
  // Remove a set of permissions from the Neuron for a given PrincipalId. If the PrincipalId has all of
  // its permissions removed, it will be removed from the neuron's permissions list. This is a dangerous
  // operation as it's possible to remove all permissions for a neuron and no longer be able to modify
  // its state, i.e. disbursing the neuron back into the governance token.
```

**File:** rs/sns/governance/api_helpers/src/lib.rs (L15-44)
```rust
pub const DEFAULT_NEURON_CLAIMER_PERMISSIONS: &[NeuronPermissionType] = &[
    NeuronPermissionType::ManagePrincipals,
    NeuronPermissionType::Vote,
    NeuronPermissionType::SubmitProposal,
];

pub fn default_nervous_system_parameters() -> NervousSystemParameters {
    NervousSystemParameters {
        reject_cost_e8s: Some(E8S_PER_TOKEN), // 1 governance token
        neuron_minimum_stake_e8s: Some(E8S_PER_TOKEN), // 1 governance token
        transaction_fee_e8s: Some(DEFAULT_TRANSFER_FEE.get_e8s()),
        max_proposals_to_keep_per_action: Some(100),
        initial_voting_period_seconds: Some(4 * ONE_DAY_SECONDS), // 4d
        wait_for_quiet_deadline_increase_seconds: Some(ONE_DAY_SECONDS), // 1d
        default_followees: Some(DefaultFollowees {
            followees: btreemap! {},
        }),
        max_number_of_neurons: Some(200_000),
        neuron_minimum_dissolve_delay_to_vote_seconds: Some(6 * ONE_MONTH_SECONDS), // 6m
        max_followees_per_function: Some(15),
        max_dissolve_delay_seconds: Some(8 * ONE_YEAR_SECONDS), // 8y
        max_neuron_age_for_age_bonus: Some(4 * ONE_YEAR_SECONDS), // 4y
        max_number_of_proposals_with_ballots: Some(700),
        neuron_claimer_permissions: Some(NeuronPermissionList {
            permissions: DEFAULT_NEURON_CLAIMER_PERMISSIONS
                .iter()
                .map(|p| *p as i32)
                .collect(),
        }),
        neuron_grantable_permissions: Some(NeuronPermissionList::default()),
```
