### Title
Lack of Name Normalization in `RegisterKnownNeuron` Proposal Allows Homoglyph/Case-Variant Squatting - (File: `rs/nns/governance/src/proposals/register_known_neuron.rs`)

### Summary
The NNS Governance canister's `RegisterKnownNeuron` proposal action does not normalize or validate the uniqueness of neuron names beyond an exact byte-for-byte string comparison. An attacker with sufficient ICP stake can register a known neuron whose name is visually indistinguishable from an existing trusted known neuron (e.g., using Unicode homoglyphs, mixed case, or lookalike whitespace), causing governance participants to follow or trust the wrong neuron.

### Finding Description
In `KnownNeuron::validate()`, the name uniqueness check is performed by an exact string lookup:

```rust
if let Some(existing_neuron_id) =
    neuron_store.known_neuron_id_by_name(&known_neuron_data.name)
    && existing_neuron_id != neuron_id
{
    return Err(...);
}
``` [1](#0-0) 

This lookup delegates to `KnownNeuronIndex::known_neuron_id_by_name`, which wraps the raw input string in `KnownNeuronName::new()` and performs a `StableBTreeMap` key lookup:

```rust
pub fn known_neuron_id_by_name(&self, name: &str) -> Option<NeuronId> {
    KnownNeuronName::new(name)
        .and_then(|known_neuron_name| self.known_neuron_name_to_id.get(&known_neuron_name))
}
``` [2](#0-1) 

`KnownNeuronName::new()` applies no normalization whatsoever — it only checks the byte length:

```rust
impl KnownNeuronName {
    fn new(name: &str) -> Option<Self> {
        if name.len() > KNOWN_NEURON_NAME_MAX_LEN {
            None
        } else {
            Some(Self(name.to_string()))
        }
    }
}
``` [3](#0-2) 

The name is stored verbatim in stable memory as a raw `String`: [4](#0-3) 

The validation in `KnownNeuron::validate()` checks only non-empty and max-length, with no case folding, Unicode normalization (NFC/NFKC), or homoglyph rejection: [5](#0-4) 

Contrast this with `validate_token_name()` in the SNS ledger validation, which at least strips whitespace and applies a lowercase banned-name check — but even that does not apply to known neuron names: [6](#0-5) 

### Impact Explanation
A malicious actor can submit a `RegisterKnownNeuron` governance proposal with a name that is visually identical to an existing trusted known neuron — for example, substituting a Cyrillic `с` for a Latin `c`, or `"DFINITY "` (trailing space) for `"DFINITY"`, or `"Dfinity"` for `"dfinity"`. If the proposal passes, the impersonating neuron appears in `list_known_neurons` alongside the legitimate one. Governance participants who follow known neurons by name, or who inspect the list visually, may follow the attacker's neuron instead of the legitimate one, distorting NNS voting outcomes. The `KnownNeuronData.name` field is a free-form `string` with no character-set restriction: [7](#0-6) 

### Likelihood Explanation
Submitting a `RegisterKnownNeuron` proposal requires only that the caller controls a neuron with sufficient dissolve delay and stake to make a proposal — a permissionless action available to any ICP holder. The proposal must then pass a governance vote, which is a meaningful barrier; however, voters reviewing a proposal with a name like `"DFINlTY"` (lowercase `l`) or `"DFINІTY"` (Cyrillic `І`) may not detect the substitution in the rendered UI. The attack is realistic for any actor willing to acquire the minimum ICP stake required to submit a proposal. [8](#0-7) 

### Recommendation
In `KnownNeuron::validate()`, before the uniqueness check, apply Unicode NFKC normalization and case-fold the name, then reject any name whose normalized form collides with an existing known neuron's normalized name. At minimum:
- Reject names that differ from their Unicode NFC/NFKC normalized form.
- Reject names with leading or trailing whitespace.
- Perform the uniqueness check against a case-folded, normalized form of all existing names.

Update `KnownNeuronName::new()` to store the normalized form, or maintain a parallel normalized-name index for collision detection.

### Proof of Concept
1. Neuron A is registered as a known neuron with name `"DFINITY"`.
2. Attacker controls Neuron B and submits a `RegisterKnownNeuron` proposal with name `"DFINlTY"` (Latin lowercase `l` replacing uppercase `I`) or `"DFINІTY"` (Cyrillic `І`).
3. `KnownNeuron::validate()` calls `neuron_store.known_neuron_id_by_name("DFINlTY")`, which performs an exact `StableBTreeMap` lookup and finds no collision with `"DFINITY"`.
4. Validation passes; if the proposal is adopted, both names coexist in `list_known_neurons`.
5. Users following known neurons by name, or inspecting the list in a UI, cannot distinguish the two entries. [9](#0-8) [10](#0-9)

### Citations

**File:** rs/nns/governance/src/proposals/register_known_neuron.rs (L59-76)
```rust
        // Validate name length
        if known_neuron_data.name.is_empty() {
            return Err(GovernanceError::new_with_message(
                ErrorType::InvalidProposal,
                "The neuron's name is empty.",
            ));
        }
        if known_neuron_data.name.len() > KNOWN_NEURON_NAME_MAX_LEN {
            return Err(GovernanceError::new_with_message(
                ErrorType::InvalidProposal,
                format!(
                    "The maximum number of bytes for a neuron's name, which is {}, \
                    has been exceeded. Current length: {}",
                    KNOWN_NEURON_NAME_MAX_LEN,
                    known_neuron_data.name.len()
                ),
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

**File:** rs/nns/governance/src/known_neuron_index.rs (L58-74)
```rust
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

**File:** rs/nns/governance/src/known_neuron_index.rs (L114-117)
```rust
    pub fn known_neuron_id_by_name(&self, name: &str) -> Option<NeuronId> {
        KnownNeuronName::new(name)
            .and_then(|known_neuron_name| self.known_neuron_name_to_id.get(&known_neuron_name))
    }
```

**File:** rs/nns/governance/src/known_neuron_index.rs (L134-145)
```rust
#[derive(Clone, Eq, PartialEq, Ord, PartialOrd, Debug)]
struct KnownNeuronName(String);

impl KnownNeuronName {
    fn new(name: &str) -> Option<Self> {
        if name.len() > KNOWN_NEURON_NAME_MAX_LEN {
            None
        } else {
            Some(Self(name.to_string()))
        }
    }
}
```

**File:** rs/nervous_system/common/src/ledger_validation.rs (L68-81)
```rust
    if token_name != token_name.trim() {
        return Err("Token name must not have leading or trailing whitespaces".to_string());
    }

    if BANNED_TOKEN_NAMES.contains(
        &token_name
            .to_lowercase()
            .chars()
            .filter(|c| !c.is_whitespace())
            .collect::<String>()
            .as_ref(),
    ) {
        return Err("Banned token name, please chose another one.".to_string());
    }
```

**File:** rs/nns/governance/src/gen/ic_nns_governance.pb.v1.rs (L2299-2308)
```rust
pub struct KnownNeuronData {
    #[prost(string, tag = "1")]
    pub name: ::prost::alloc::string::String,
    #[prost(string, optional, tag = "2")]
    pub description: ::core::option::Option<::prost::alloc::string::String>,
    #[prost(string, repeated, tag = "3")]
    pub links: ::prost::alloc::vec::Vec<::prost::alloc::string::String>,
    #[prost(enumeration = "Topic", repeated, tag = "4")]
    pub committed_topics: ::prost::alloc::vec::Vec<i32>,
}
```

**File:** rs/nns/governance/src/governance.rs (L4242-4244)
```rust
            ValidProposalAction::RegisterKnownNeuron(register_request) => {
                let result = register_request.execute(&mut self.neuron_store);
                self.set_proposal_execution_status::<()>(pid, result.map(|()| vec![]));
```
