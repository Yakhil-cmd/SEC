### Title
Case-Insensitive Known Neuron Name Collision Enables Impersonation via `RegisterKnownNeuron` - (File: `rs/nns/governance/src/proposals/register_known_neuron.rs`, `rs/nns/governance/src/known_neuron_index.rs`)

---

### Summary

The NNS Governance canister's `RegisterKnownNeuron` proposal flow does not normalize neuron names before storing or checking uniqueness. Because the `KnownNeuronName` index performs byte-exact (case-sensitive) comparisons, an attacker can register a known neuron whose name differs only in letter casing from an already-registered trusted neuron (e.g., `"DFINITY"` vs `"dfinity"`). Any NNS client or dApp that normalizes names before display will render both as identical, enabling impersonation and misdirected neuron following.

---

### Finding Description

`KnownNeuron::validate()` in `rs/nns/governance/src/proposals/register_known_neuron.rs` enforces only two constraints on the neuron name: it must be non-empty and must not exceed `KNOWN_NEURON_NAME_MAX_LEN` bytes. [1](#0-0) 

The uniqueness check that follows queries the index with the raw, un-normalized name: [2](#0-1) 

The index itself, `KnownNeuronIndex`, stores names verbatim. `KnownNeuronName::new()` performs no case folding: [3](#0-2) 

The underlying storage is a `StableBTreeMap` keyed on the raw byte representation of the name string: [4](#0-3) 

The lookup function `known_neuron_id_by_name` therefore performs a byte-exact match: [5](#0-4) 

Because `"dfinity"` and `"DFINITY"` produce different byte sequences, both can be inserted into the index as distinct entries. The duplicate-name guard in `validate()` will not fire when the second registration uses a differently-cased variant of an existing name.

---

### Impact Explanation

Known neurons are the primary mechanism by which ordinary ICP holders delegate their voting power. A user who searches for `"dfinity"` in an NNS dApp that normalizes names to lowercase will see two neurons displayed identically. The attacker's neuron, registered as `"DFINITY"`, is indistinguishable from the legitimate `"dfinity"` neuron in any case-normalizing UI. Voters who follow the wrong neuron inadvertently delegate their governance weight to the attacker, distorting NNS proposal outcomes. The impact is a **governance authorization integrity violation**: voting power is silently redirected through impersonation of a trusted known neuron. [6](#0-5) 

---

### Likelihood Explanation

The entry path is reachable by any unprivileged NNS neuron holder. Submitting a `RegisterKnownNeuron` proposal requires only a neuron with a dissolve delay above the minimum threshold (currently 6 months, or 2 weeks under Mission 70 parameters) and payment of the proposal rejection fee. [7](#0-6) 

The proposal must pass a governance vote, which raises the bar. However, the on-chain validation is the root cause: it never rejects a case-variant name regardless of the vote outcome. A well-resourced attacker with sufficient ICP staked, or one who can convince other neurons to vote yes, can execute this attack. The `RegisterKnownNeuron` topic historically passes with relatively low contention. Likelihood is **medium**.

---

### Recommendation

Normalize all known neuron names to a canonical form (e.g., Unicode case-fold or ASCII lowercase) before both the uniqueness check and storage. Concretely:

1. In `KnownNeuronName::new()`, apply `.to_lowercase()` (or Unicode NFKC case-fold) before constructing the stored key.
2. In `KnownNeuron::validate()`, apply the same normalization to `known_neuron_data.name` before calling `known_neuron_id_by_name`.
3. Decide whether the stored display name should retain the original casing (stored separately) while the index key is always normalized. [3](#0-2) 

---

### Proof of Concept

1. Neuron A (legitimate DFINITY neuron) is already registered with name `"dfinity"` via a passed `RegisterKnownNeuron` proposal.

2. Attacker controls Neuron B with ≥6 months dissolve delay. Attacker submits a `RegisterKnownNeuron` proposal for Neuron B with name `"DFINITY"`.

3. `KnownNeuron::validate()` is called. The name length check passes (7 bytes ≤ 200). The uniqueness check calls `neuron_store.known_neuron_id_by_name("DFINITY")`.

4. Inside `KnownNeuronIndex::known_neuron_id_by_name`, `KnownNeuronName::new("DFINITY")` produces the key `KnownNeuronName("DFINITY")`. The `StableBTreeMap` lookup finds no entry for this exact byte sequence (the existing entry is `KnownNeuronName("dfinity")`), so `None` is returned.

5. The uniqueness guard does not fire. Validation passes. If the proposal is adopted, Neuron B is stored in the index under key `"DFINITY"`.

6. An NNS dApp that normalizes names before display shows both `"dfinity"` (Neuron A) and `"DFINITY"` (Neuron B) as `"dfinity"`. Users who follow `"dfinity"` may select Neuron B instead of Neuron A, delegating their voting power to the attacker. [8](#0-7) [5](#0-4)

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

**File:** rs/nns/governance/src/known_neuron_index.rs (L114-117)
```rust
    pub fn known_neuron_id_by_name(&self, name: &str) -> Option<NeuronId> {
        KnownNeuronName::new(name)
            .and_then(|known_neuron_name| self.known_neuron_name_to_id.get(&known_neuron_name))
    }
```

**File:** rs/nns/governance/src/known_neuron_index.rs (L137-144)
```rust
impl KnownNeuronName {
    fn new(name: &str) -> Option<Self> {
        if name.len() > KNOWN_NEURON_NAME_MAX_LEN {
            None
        } else {
            Some(Self(name.to_string()))
        }
    }
```

**File:** rs/nns/governance/src/known_neuron_index.rs (L147-159)
```rust
impl Storable for KnownNeuronName {
    fn to_bytes(&self) -> std::borrow::Cow<'_, [u8]> {
        self.0.to_bytes()
    }

    fn from_bytes(bytes: std::borrow::Cow<[u8]>) -> Self {
        Self(String::from_bytes(bytes))
    }

    const BOUND: Bound = Bound::Bounded {
        max_size: KNOWN_NEURON_NAME_MAX_LEN as u32,
        is_fixed_size: false,
    };
```

**File:** rs/nns/governance/src/network_economics.rs (L278-283)
```rust
    pub const DEFAULT_NEURON_MINIMUM_DISSOLVE_DELAY_TO_VOTE_SECONDS: u64 = 6 * ONE_MONTH_SECONDS;

    /// The default value for `neuron_minimum_dissolve_delay_to_vote_seconds` once the mission 70
    /// voting rewards feature is enabled. Two weeks instead of six months.
    pub const MISSION_70_DEFAULT_NEURON_MINIMUM_DISSOLVE_DELAY_TO_VOTE_SECONDS: u64 =
        14 * ONE_DAY_SECONDS;
```
