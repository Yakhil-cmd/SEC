### Title
Known Neuron Name Squatting via Concurrent `RegisterKnownNeuron` Proposals - (`rs/nns/governance/src/proposals/register_known_neuron.rs`)

---

### Summary

The NNS governance canister enforces known-neuron name uniqueness only against the *committed* state at proposal-submission time. Because two `RegisterKnownNeuron` proposals for different neuron IDs but the same name can both pass submission-time validation and be simultaneously pending, a malicious neuron holder can observe a legitimate pending proposal and submit a competing proposal with the identical name. Whichever proposal executes second will fail, permanently denying the legitimate neuron owner their chosen name and burning their proposal fee.

---

### Finding Description

`KnownNeuron::validate` in `rs/nns/governance/src/proposals/register_known_neuron.rs` checks name uniqueness by querying the `NeuronStore` index:

```rust
if let Some(existing_neuron_id) =
    neuron_store.known_neuron_id_by_name(&known_neuron_data.name)
    && existing_neuron_id != neuron_id
{
    return Err(GovernanceError::new_with_message(
        ErrorType::PreconditionFailed,
        format!(
            "The name '{}' already belongs to a different known neuron with ID {}",
            known_neuron_data.name, existing_neuron_id.id
        ),
    ));
}
``` [1](#0-0) 

This check is invoked at proposal-submission time via `validate_proposal_action`:

```rust
ValidProposalAction::RegisterKnownNeuron(register_known_neuron) => {
    register_known_neuron.validate(&self.neuron_store)
}
``` [2](#0-1) 

The same `validate` call is repeated inside `execute` before writing state:

```rust
pub fn execute(&self, neuron_store: &mut NeuronStore) -> Result<(), GovernanceError> {
    // Validate again for safety
    self.validate(neuron_store)?;
``` [3](#0-2) 

The critical gap: **there is no check for other pending proposals that already claim the same name**. The `KnownNeuronIndex` only reflects names that have already been committed to state: [4](#0-3) 

Consequently, two proposals — P1 for neuron A with name "X" and P2 for neuron B with name "X" — can both pass submission-time validation when neither name is yet committed. Whichever proposal's `execute` runs second will hit the `PreconditionFailed` error and be marked `Failed`.

Additionally, `validate` does not verify that the proposing neuron is the controller of the neuron being registered. Any neuron holder can submit a `RegisterKnownNeuron` proposal for any existing neuron ID: [5](#0-4) 

---

### Impact Explanation

A malicious neuron holder can:

1. Monitor the public list of pending NNS proposals for any `RegisterKnownNeuron` action.
2. Immediately submit a competing proposal for their own neuron using the identical name.
3. Rely on liquid democracy (neuron following) to have their proposal adopted — voters following popular neurons may adopt both proposals without noticing the name collision.
4. If their proposal executes first, the legitimate neuron owner's proposal fails at execution time, the proposal fee is burned, and the attacker's neuron permanently holds the name.

This enables griefing of high-profile neuron owners (e.g., DFINITY, ICA, major node providers) who are trying to register recognizable names, and could be used to impersonate them in the known-neuron list, influencing liquid-democracy following decisions across the NNS. [6](#0-5) 

---

### Likelihood Explanation

- All NNS proposals are publicly visible immediately after submission; no mempool observation is needed.
- Any principal holding a neuron with sufficient dissolve delay can submit a `RegisterKnownNeuron` proposal.
- NNS liquid democracy means many neurons follow a small set of known neurons; both competing proposals may be adopted without explicit per-proposal review by most voters.
- The proposal rejection fee is a modest deterrent but not a meaningful barrier for a motivated attacker.
- The attack is repeatable: after the legitimate owner resubmits with a new name, the attacker can repeat the squatting.

---

### Recommendation

1. **Reject duplicate pending proposals at submission time**: When a `RegisterKnownNeuron` proposal is submitted, scan all open proposals for any other `RegisterKnownNeuron` action that claims the same name for a different neuron ID, and reject the new submission immediately.

2. **Bind the proposal to the proposer's neuron**: Require that the neuron ID in the `RegisterKnownNeuron` action matches the neuron submitting the proposal (`manage_neuron` caller). This prevents any neuron from squatting a name on behalf of a neuron it does not control.

3. **Reserve the name at proposal-open time**: Insert a tentative entry in `KnownNeuronIndex` when the proposal is opened (not when it executes), and remove it if the proposal is rejected or fails. This makes the name unavailable to concurrent proposals.

---

### Proof of Concept

1. Neuron A (controlled by a legitimate organization) submits proposal P1:
   ```
   RegisterKnownNeuron { id: NeuronId { id: 42 }, known_neuron_data: KnownNeuronData { name: "DFINITY Foundation", ... } }
   ```
   P1 passes `validate` because `known_neuron_id_by_name("DFINITY Foundation")` returns `None`.

2. Attacker (neuron B, id: 999) immediately submits proposal P2:
   ```
   RegisterKnownNeuron { id: NeuronId { id: 999 }, known_neuron_data: KnownNeuronData { name: "DFINITY Foundation", ... } }
   ```
   P2 also passes `validate` because the name is still not committed.

3. Both P1 and P2 enter the voting period. Liquid democracy causes both to be adopted.

4. P2 executes first (e.g., it was submitted slightly earlier and processed first in the same round, or the governance timer processes it first). `execute` writes `"DFINITY Foundation" → neuron 999` into `KnownNeuronIndex`.

5. P1 executes second. `execute` calls `self.validate(neuron_store)`, which now finds `known_neuron_id_by_name("DFINITY Foundation") == Some(NeuronId { id: 999 })` and `999 != 42`, returning `PreconditionFailed`. P1 is marked `Failed`.

6. Neuron 999 (attacker) now appears in the known-neuron list as "DFINITY Foundation", influencing all neurons that follow by name. [1](#0-0) [3](#0-2) [7](#0-6)

### Citations

**File:** rs/nns/governance/src/proposals/register_known_neuron.rs (L36-50)
```rust
    pub fn validate(&self, neuron_store: &NeuronStore) -> Result<(), GovernanceError> {
        let neuron_id = self.id.ok_or_else(|| {
            GovernanceError::new_with_message(
                ErrorType::InvalidProposal,
                "No neuron ID specified in the request to register a known neuron.",
            )
        })?;

        // Check that the neuron exists
        if !neuron_store.contains(neuron_id) {
            return Err(GovernanceError::new_with_message(
                ErrorType::NotFound,
                format!("Neuron {} not found", neuron_id.id),
            ));
        }
```

**File:** rs/nns/governance/src/proposals/register_known_neuron.rs (L128-143)
```rust
        // Check that the name is not already used by another known neuron
        // Allow registration if:
        // - No existing known neuron has this name (None), OR
        // - An existing known neuron has this name but it's the same neuron ID (clobbering OK)
        if let Some(existing_neuron_id) =
            neuron_store.known_neuron_id_by_name(&known_neuron_data.name)
            && existing_neuron_id != neuron_id
        {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "The name '{}' already belongs to a different known neuron with ID {}",
                    known_neuron_data.name, existing_neuron_id.id
                ),
            ));
        }
```

**File:** rs/nns/governance/src/proposals/register_known_neuron.rs (L148-176)
```rust
    /// Executes the register known neuron action.
    ///
    /// This method adds the known neuron data (name, description, and links) to the neuron,
    /// making it a known neuron. The validation is performed again during execution for safety.
    pub fn execute(&self, neuron_store: &mut NeuronStore) -> Result<(), GovernanceError> {
        // Validate again for safety
        self.validate(neuron_store)?;

        let neuron_id = self.id.ok_or_else(|| {
            GovernanceError::new_with_message(
                ErrorType::InvalidProposal,
                "No neuron ID specified in the request to register a known neuron.",
            )
        })?;

        let known_neuron_data = self.known_neuron_data.as_ref().ok_or_else(|| {
            GovernanceError::new_with_message(
                ErrorType::InvalidProposal,
                "No known neuron data specified in the register neuron request.",
            )
        })?;

        // Set the known neuron data
        neuron_store.with_neuron_mut(&neuron_id, |neuron| {
            neuron.set_known_neuron_data(known_neuron_data.clone())
        })?;

        Ok(())
    }
```

**File:** rs/nns/governance/src/governance.rs (L4899-4901)
```rust
            ValidProposalAction::RegisterKnownNeuron(register_known_neuron) => {
                register_known_neuron.validate(&self.neuron_store)
            }
```

**File:** rs/nns/governance/src/known_neuron_index.rs (L8-15)
```rust
/// An index to make it easy to check whether a known neuron with the same name exists,
/// as well as listing all known neuron's ids.
/// Note that the index only cares about the uniqueness of the names, not the ids -
/// the caller should make sure the name-id is removed from the index when a neuron
/// is removed or its name is changed.
pub struct KnownNeuronIndex<M: Memory> {
    known_neuron_name_to_id: StableBTreeMap<KnownNeuronName, NeuronId, M>,
}
```

**File:** rs/nns/governance/src/known_neuron_index.rs (L52-74)
```rust
    /// Adds a known neuron to the index. Returns error if nothing is added.
    /// The reason the known neuron might not gets added into the index might be that:
    /// (1) the known neuron name already exists for a different neuron id (caller should call
    /// `known_neuron_id_by_name` first)
    /// (2) the known neuron name exceeds the maximum size.
    /// In both cases, the clients should check the condition before adding to the index.
    pub fn add_known_neuron(
        &mut self,
        name: &str,
        neuron_id: NeuronId,
    ) -> Result<(), AddKnownNeuronError> {
        let known_neuron_name =
            KnownNeuronName::new(name).ok_or(AddKnownNeuronError::ExceedsSizeLimit)?;
        if self
            .known_neuron_name_to_id
            .contains_key(&known_neuron_name)
        {
            return Err(AddKnownNeuronError::AlreadyExists);
        }
        self.known_neuron_name_to_id
            .insert(known_neuron_name, neuron_id);
        Ok(())
    }
```
