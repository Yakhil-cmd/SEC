### Title
Invalid `==` Cap Check in `add_neuron_permissions` Allows `max_number_of_principals_per_neuron` to Be Exceeded — (`rs/sns/governance/src/governance.rs`)

---

### Summary

In `rs/sns/governance/src/governance.rs`, the `add_neuron_permissions` function enforces the `max_number_of_principals_per_neuron` cap using a strict equality check (`==`) instead of a greater-than-or-equal check (`>=`). If the governance parameter `max_number_of_principals_per_neuron` is reduced via a `ManageNervousSystemParameters` proposal after neurons have already reached the old cap, the equality check is silently bypassed for all neurons whose `permissions.len()` now exceeds the new cap, allowing unlimited additional principals to be added.

---

### Finding Description

In `rs/sns/governance/src/governance.rs`, the `add_neuron_permissions` function reads the current `max_number_of_principals_per_neuron` from the live nervous system parameters and then guards against exceeding it:

```rust
// rs/sns/governance/src/governance.rs ~L4619-4630
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
```

The condition uses `==` (equality) rather than `>=` (greater-than-or-equal). This is safe only when `neuron.permissions.len()` can never exceed `max_number_of_principals_per_neuron`. However, `max_number_of_principals_per_neuron` is a mutable governance parameter — it can be lowered at any time via a `ManageNervousSystemParameters` proposal. When it is lowered, any neuron whose `permissions.len()` already exceeds the new cap will have `permissions.len() > max_number_of_principals_per_neuron`, making the `==` condition evaluate to `false`. The guard is silently skipped, and the caller can keep adding new principals indefinitely.

The `max_number_of_principals_per_neuron` parameter is validated to be between `MAX_NUMBER_OF_PRINCIPALS_PER_NEURON_FLOOR` (5) and `MAX_NUMBER_OF_PRINCIPALS_PER_NEURON_CEILING` (15), so a governance proposal can legitimately reduce it from 15 to 5 while neurons already hold 15 principals. [1](#0-0) 

The parameter bounds are defined here: [2](#0-1) 

---

### Impact Explanation

Once the cap is bypassed, a neuron controller or any principal holding `ManagePrincipals` permission can add an unbounded number of principals to a neuron's `permissions` vector. The `permissions` field is stored in the SNS governance canister's heap-allocated `proto.neurons` map. Unbounded growth of this vector across many neurons can exhaust the canister's heap memory (capped at ~4 GiB on a 32-bit Wasm heap), causing the governance canister to trap on allocation and become permanently non-functional — a denial-of-service against the entire SNS.

Even short of full exhaustion, neurons with bloated `permissions` vectors increase the cost of every operation that iterates over permissions (voting, permission checks, serialization), degrading canister performance for all users. [3](#0-2) 

---

### Likelihood Explanation

The trigger requires two steps: (1) a governance proposal to lower `max_number_of_principals_per_neuron`, and (2) a neuron controller calling `AddNeuronPermissions` on a neuron that already holds more principals than the new cap. Step (1) is a normal, legitimate governance action (e.g., tightening security policy). Step (2) requires only the `ManagePrincipals` permission on the target neuron, which is held by the neuron's controller and any granted hot key — both are unprivileged ingress callers. No threshold corruption, admin key, or social engineering is required. The combination is realistic in any SNS that has ever changed its principal-per-neuron limit.

---

### Recommendation

Change the equality check to a greater-than-or-equal check so that neurons already over the cap are also blocked:

```rust
// Before (vulnerable):
if existing_permissions.is_none()
    && neuron.permissions.len() == max_number_of_principals_per_neuron as usize

// After (correct):
if existing_permissions.is_none()
    && neuron.permissions.len() >= max_number_of_principals_per_neuron as usize
```

This mirrors the correct pattern already used in `claim_swap_neurons` validation: [4](#0-3) 

---

### Proof of Concept

1. Deploy an SNS with `max_number_of_principals_per_neuron = 10`.
2. Neuron A's controller calls `AddNeuronPermissions` nine times, adding nine distinct principals. `neuron.permissions.len()` reaches 10 (the cap). The tenth call is correctly rejected.
3. A governance proposal passes that sets `max_number_of_principals_per_neuron = 5`.
4. Neuron A's controller calls `AddNeuronPermissions` with a new principal (principal #11).
5. Inside `add_neuron_permissions`: `existing_permissions.is_none()` is `true` (new principal), and `neuron.permissions.len() == max_number_of_principals_per_neuron` evaluates to `10 == 5` → `false`. The guard is skipped.
6. `add_permissions_for_principal` is called, pushing the 11th entry into `neuron.permissions`.
7. Repeat step 4–6 indefinitely to grow `permissions` without bound. [5](#0-4)

### Citations

**File:** rs/sns/governance/src/governance.rs (L4614-4643)
```rust
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

**File:** rs/sns/governance/src/types.rs (L412-421)
```rust
    /// This is an upper bound for `max_number_of_principals_per_neuron`. Exceeding
    /// it may cause may cause degradation in the governance canister or the subnet
    /// hosting the SNS.
    pub const MAX_NUMBER_OF_PRINCIPALS_PER_NEURON_CEILING: u64 = 15;

    /// This is a lower bound for `max_number_of_principals_per_neuron`.
    /// Decreasing it below this number is problematic because SNS Swap assumes
    /// that there are allowed to be at least 5 principals per
    /// neuron during ClaimSwapNeuronsRequest.
    pub const MAX_NUMBER_OF_PRINCIPALS_PER_NEURON_FLOOR: u64 = 5;
```

**File:** rs/sns/governance/src/types.rs (L2273-2283)
```rust
        match self.construct_permissions(NeuronPermissionList::default()) {
            Ok(permissions) => {
                if permissions.len() > max_number_of_principals_per_neuron as usize {
                    defects.push(format!(
                        "Neuron recipe would correspond to a neuron with ({}) permissions ({:?}), exceeding the maximum \
                            number of permissions ({})",
                        permissions.len(),
                        permissions,
                        max_number_of_principals_per_neuron
                    ));
                }
```
