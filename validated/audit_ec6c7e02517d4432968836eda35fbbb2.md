### Title
Inconsistent Deletion-Marker Check Allows Re-Adding a Removed `NervousSystemFunction` via Stale Proposal — (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS governance canister's execution path for `AddGenericNervousSystemFunction` uses `is_registered_function_id`, which returns `false` for deletion-marker entries, while the proposal-validation path uses `contains_key`, which correctly returns `true` for those same entries. A proposal to add function X that was submitted and adopted before X was removed will, upon execution, bypass the deletion-marker guard and re-insert the function — undoing the community's removal decision.

---

### Finding Description

When a `NervousSystemFunction` is removed via `perform_remove_generic_nervous_system_function`, the entry is not deleted from `id_to_nervous_system_functions`; instead a `NERVOUS_SYSTEM_FUNCTION_DELETION_MARKER` is written at that key to prevent ID recycling: [1](#0-0) 

The **validation** function `validate_and_render_add_generic_nervous_system_function` guards against re-adding by calling `contains_key`, which returns `true` for the deletion-marker entry: [2](#0-1) 

The **execution** function `perform_add_generic_nervous_system_function` guards against re-adding by calling `is_registered_function_id`: [3](#0-2) 

However, `is_registered_function_id` explicitly returns `false` when the stored value equals the deletion marker: [4](#0-3) 

Because the execution guard evaluates to `false` for a deletion-marker entry, `perform_add_generic_nervous_system_function` proceeds to insert the function back into the map: [5](#0-4) 

The two layers are therefore inconsistent: validation blocks re-addition, execution does not.

---

### Impact Explanation

A `NervousSystemFunction` that the SNS community explicitly voted to remove can be silently re-registered. Once re-registered, any neuron with sufficient voting power can submit `ExecuteGenericNervousSystemFunction` proposals that call the function's `target_canister_id` / `target_method_name`. Depending on what that method does (e.g., treasury transfers, canister upgrades, parameter changes), the impact ranges from governance-process subversion to direct financial or operational harm to the SNS.

---

### Likelihood Explanation

The race requires:

1. A proposal to add function X is submitted and **adopted** (passes voting) while X does not yet exist.
2. A separate proposal to remove X is submitted, adopted, and **executed** — inserting the deletion marker.
3. The original add-proposal is then **executed** (heartbeat fires after step 2).

SNS proposals are executed in the governance heartbeat, not atomically with the vote tally. A window therefore exists between adoption and execution. An actor with enough voting power to pass the add-proposal quickly (e.g., a whale neuron or a coordinated group) can engineer this ordering. The likelihood is **low-to-medium**: it requires deliberate timing but no privileged access beyond normal neuron voting power.

---

### Recommendation

Replace the `is_registered_function_id` guard in `perform_add_generic_nervous_system_function` with a `contains_key` check (matching the validation layer), so that any entry — active function or deletion marker — blocks re-insertion:

```rust
// Current (incorrect):
if is_registered_function_id(id, &self.proto.id_to_nervous_system_functions) { … }

// Corrected:
if self.proto.id_to_nervous_system_functions.contains_key(&id) { … }
```

Additionally, `validate_and_render_remove_nervous_generic_system_function` should explicitly reject proposals targeting an already-deleted ID (currently it returns `Ok` for deletion-marker entries, enabling no-op governance spam): [6](#0-5) 

---

### Proof of Concept

```
T0  Attacker submits Proposal A: AddGenericNervousSystemFunction { id: 1000, … }
    → validate_and_render_add_generic_nervous_system_function:
        existing_functions.contains_key(&1000) == false  ✓ passes

T1  Proposal A is adopted (voting period ends, quorum reached).
    Execution is queued for the next heartbeat.

T2  Community submits Proposal B: RemoveGenericNervousSystemFunction(1000)
    Proposal B is adopted and executed in the same heartbeat:
        id_to_nervous_system_functions[1000] = DELETION_MARKER

T3  Heartbeat fires; Proposal A is executed:
        perform_add_generic_nervous_system_function(id=1000):
            is_registered_function_id(1000, …)
              → nervous_system_functions.get(&1000) == Some(DELETION_MARKER)
              → DELETION_MARKER != DELETION_MARKER? No — returns false  ← BUG
            Guard does NOT fire.
            id_to_nervous_system_functions.insert(1000, nervous_system_function)
            ← Function 1000 is live again despite community removal.
```

The removed function is now executable via `ExecuteGenericNervousSystemFunction` proposals, bypassing the community's governance decision.

### Citations

**File:** rs/sns/governance/src/governance.rs (L2261-2269)
```rust
        if is_registered_function_id(id, &self.proto.id_to_nervous_system_functions) {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "Failed to add NervousSystemFunction. \
                             There is/was already a NervousSystemFunction with id: {id}"
                ),
            ));
        }
```

**File:** rs/sns/governance/src/governance.rs (L2295-2298)
```rust
        self.proto
            .id_to_nervous_system_functions
            .insert(id, nervous_system_function);
        Ok(())
```

**File:** rs/sns/governance/src/governance.rs (L2315-2319)
```rust
            Entry::Occupied(mut o) => {
                // Insert a deletion marker to signify that there was a NervousSystemFunction
                // with this id at some point, but that it was deleted.
                o.insert(NERVOUS_SYSTEM_FUNCTION_DELETION_MARKER.clone());
                Ok(())
```

**File:** rs/sns/governance/src/proposal.rs (L1379-1384)
```rust
    if existing_functions.contains_key(&validated_function.id) {
        return Err(format!(
            "There is already a NervousSystemFunction with id: {}",
            validated_function.id
        ));
    }
```

**File:** rs/sns/governance/src/proposal.rs (L1415-1429)
```rust
pub fn validate_and_render_remove_nervous_generic_system_function(
    remove: u64,
    existing_functions: &BTreeMap<u64, NervousSystemFunction>,
) -> Result<String, String> {
    match existing_functions.get(&remove) {
        None => Err(format!("NervousSystemFunction: {remove} doesn't exist")),
        Some(function) => Ok(format!(
            r"# Proposal to remove existing NervousSystemFunction:

## Function:

{function:#?}"
        )),
    }
}
```

**File:** rs/sns/governance/src/types.rs (L2011-2014)
```rust
    match nervous_system_functions.get(&function_id) {
        None => false,
        Some(function) => function != &*NERVOUS_SYSTEM_FUNCTION_DELETION_MARKER,
    }
```
