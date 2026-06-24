### Title
`NervousSystemParameters::neuron_grantable_permissions` Not Validated for Duplicate Entries - (File: rs/sns/governance/src/types.rs)

### Summary
The `validate_neuron_grantable_permissions` function in SNS Governance only checks that the field is present (non-`None`), but does not validate that the `NeuronPermissionList::permissions` vector is a unique set. A governance proposal can set `neuron_grantable_permissions` to a list containing duplicate permission integers, which then corrupts the `check_permissions_are_grantable` subset check used to gate every `AddNeuronPermissions` call.

### Finding Description
`NervousSystemParameters::validate_neuron_grantable_permissions` performs only a presence check:

```rust
// rs/sns/governance/src/types.rs:869-876
fn validate_neuron_grantable_permissions(&self) -> Result<(), String> {
    self.neuron_grantable_permissions.as_ref().ok_or_else(|| {
        "NervousSystemParameters.neuron_grantable_permissions must be set".to_string()
    })?;
    Ok(())
}
```

No check is made that the `permissions` vector inside `NeuronPermissionList` is a unique set. [1](#0-0) 

By contrast, `validate_neuron_claimer_permissions` converts the list to a `BTreeSet<NeuronPermissionType>` via `try_into()`, which implicitly deduplicates, but `validate_neuron_grantable_permissions` never does this. [2](#0-1) 

The downstream consumer `check_permissions_are_grantable` builds a `HashSet<&i32>` from the raw `permissions` vector of `neuron_grantable_permissions`. Because a `HashSet` is used, duplicates in the stored list are silently collapsed at check time, but the stored `NeuronPermissionList` itself remains malformed. [3](#0-2) 

The `NervousSystemParameters` are updated via a `ManageNervousSystemParameters` governance proposal, which calls `perform_manage_nervous_system_parameters` → `new_params.validate()`. Since `validate_neuron_grantable_permissions` only checks for presence, a proposal carrying `neuron_grantable_permissions: [Vote, Vote, Vote, ...]` (all duplicates) passes validation and is stored. [4](#0-3) 

The `add_neuron_permissions` handler enforces a size guard against DoS:

```rust
// rs/sns/governance/src/governance.rs:4588-4594
if permissions_to_add.permissions.len() > NeuronPermissionType::all().len() {
    return Err(...);
}
```

This guard compares the *caller-supplied* list length against the total number of distinct permission types. However, if `neuron_grantable_permissions` itself is stored with duplicates, the `check_permissions_are_grantable` subset check becomes semantically incorrect: the effective grantable set is smaller than the stored list implies, and the stored list's length no longer accurately represents the number of distinct grantable permissions. [5](#0-4) 

### Impact Explanation
An SNS community that passes a `ManageNervousSystemParameters` proposal with a `neuron_grantable_permissions` list containing duplicates will store a malformed permission list. While `check_permissions_are_grantable` collapses duplicates at check time (via `HashSet`), the stored state is inconsistent with the intended semantics of `neuron_grantable_permissions` as a *set* of grantable permissions. This can lead to:

1. **Governance authorization confusion**: Any code that iterates or counts `neuron_grantable_permissions.permissions` directly (e.g., length comparisons, serialization, display) will see inflated counts, misrepresenting the actual permission policy.
2. **Unexpected behavior in future use**: If any future code path relies on the stored list being a unique set (analogous to the original audit finding), the duplicate entries will cause incorrect behavior without any prior warning.
3. **Inconsistency with `neuron_claimer_permissions`**: The claimer permissions list is validated via `BTreeSet` conversion (deduplication enforced), but the grantable permissions list is not, creating an asymmetric and surprising invariant.

### Likelihood Explanation
The entry path is a governance proposal submitted by any SNS neuron holder with `SubmitProposal` permission — an unprivileged ingress sender. The proposal passes on-chain validation because `validate_neuron_grantable_permissions` only checks for presence. The likelihood is low in practice (requires a malicious or buggy proposer), but the attack surface is fully reachable without any privileged access.

### Recommendation
`validate_neuron_grantable_permissions` should validate that the `permissions` vector contains no duplicates and no invalid (unknown) permission integers, mirroring the approach used in `validate_neuron_claimer_permissions`:

```rust
fn validate_neuron_grantable_permissions(&self) -> Result<(), String> {
    let list = self.neuron_grantable_permissions.as_ref().ok_or_else(|| {
        "NervousSystemParameters.neuron_grantable_permissions must be set".to_string()
    })?;
    // Validate and deduplicate
    let set: BTreeSet<NeuronPermissionType> = list.clone().try_into()?;
    if set.len() != list.permissions.len() {
        return Err("NervousSystemParameters.neuron_grantable_permissions must not contain duplicates".to_string());
    }
    Ok(())
}
```

### Proof of Concept
1. An SNS neuron holder submits a `ManageNervousSystemParameters` proposal with:
   ```
   neuron_grantable_permissions = NeuronPermissionList {
       permissions: [Vote, Vote, Vote, Vote, Vote, Vote, Vote, Vote, Vote, Vote, Vote]
       // 11 entries, all duplicates of Vote (i32 = 4)
   }
   ```
2. `perform_manage_nervous_system_parameters` calls `new_params.validate()`. [6](#0-5) 
3. `validate_neuron_grantable_permissions` only checks `is_some()` — passes. [7](#0-6) 
4. The malformed list is stored in `self.proto.parameters`.
5. Any subsequent call to `add_neuron_permissions` with `permissions_to_add.len() <= NeuronPermissionType::all().len()` (≤10) passes the DoS guard, and `check_permissions_are_grantable` collapses the stored duplicates silently via `HashSet`, masking the stored inconsistency. [8](#0-7) 
6. The stored `neuron_grantable_permissions` now has `permissions.len() == 11` while representing only 1 distinct permission, violating the set invariant and creating a persistent state inconsistency observable by any caller reading `get_nervous_system_parameters`.

### Citations

**File:** rs/sns/governance/src/types.rs (L833-857)
```rust
    /// Validates that the nervous system parameter neuron_claimer_permissions is well-formed.
    fn validate_neuron_claimer_permissions(&self) -> Result<(), String> {
        let neuron_claimer_permissions =
            self.neuron_claimer_permissions.as_ref().ok_or_else(|| {
                "NervousSystemParameters.neuron_claimer_permissions must be set".to_string()
            })?;

        let neuron_claimer_permissions = neuron_claimer_permissions.clone().try_into().unwrap();

        let required_claimer_permissions = Self::REQUIRED_NEURON_CLAIMER_PERMISSIONS
            .iter()
            .cloned()
            .collect::<BTreeSet<_>>();

        let difference = required_claimer_permissions
            .difference(&neuron_claimer_permissions)
            .collect::<Vec<_>>();

        if !difference.is_empty() {
            return Err(format!(
                "NervousSystemParameters.neuron_claimer_permissions is missing the required permissions {difference:?}",
            ));
        }
        Ok(())
    }
```

**File:** rs/sns/governance/src/types.rs (L869-876)
```rust
    /// Validates that the nervous system parameter neuron_grantable_permissions is well-formed.
    fn validate_neuron_grantable_permissions(&self) -> Result<(), String> {
        self.neuron_grantable_permissions.as_ref().ok_or_else(|| {
            "NervousSystemParameters.neuron_grantable_permissions must be set".to_string()
        })?;

        Ok(())
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

**File:** rs/sns/governance/src/governance.rs (L2579-2617)
```rust
    /// Executes a ManageNervousSystemParameters proposal by updating Governance's
    /// NervousSystemParameters
    fn perform_manage_nervous_system_parameters(
        &mut self,
        proposed_params: NervousSystemParameters,
    ) -> Result<(), GovernanceError> {
        // Only set `self.proto.parameters` if "applying" the proposed params to the
        // current params results in valid params
        let new_params = proposed_params.inherit_from(self.nervous_system_parameters_or_panic());

        log!(
            INFO,
            "Setting Governance nervous system params to: {:?}",
            &new_params
        );

        match new_params.validate() {
            Ok(()) => {
                self.proto.parameters = Some(new_params);
                Ok(())
            }

            // Even though proposals are validated when they are first made, this is still
            // possible, because the inner value of a ManageNervousSystemParameters
            // proposal is only valid with respect to the current
            // nervous_system_parameters() at the time when the proposal was first
            // made. If nervous_system_parameters() changed (by another proposal) since
            // the current proposal was first made, the current proposal might have become
            // invalid. Basically, this might occur if there are conflicting (concurrent)
            // proposals, but we expect this to be highly unusual in practice.
            Err(msg) => Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "Failed to perform ManageNervousSystemParameters action, proposed \
                        parameters would lead to invalid NervousSystemParameters: {msg}"
                ),
            )),
        }
    }
```

**File:** rs/sns/governance/src/governance.rs (L4588-4594)
```rust
        // A simple check to prevent DoS attack with large number of permission changes.
        if permissions_to_add.permissions.len() > NeuronPermissionType::all().len() {
            return Err(GovernanceError::new_with_message(
                ErrorType::InvalidCommand,
                "AddNeuronPermissions command provided more permissions than exist in the system",
            ));
        }
```
