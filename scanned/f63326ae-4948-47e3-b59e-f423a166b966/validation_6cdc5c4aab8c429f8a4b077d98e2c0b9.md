### Title
Hot Keys Not Cleared on `spawn_neuron` with Different Controller — (`rs/nns/governance/src/governance.rs`)

### Summary
When `spawn_neuron` is called with a `new_controller` that differs from the parent neuron's controller, the child neuron unconditionally inherits the parent's full `hot_keys` list. The original neuron owner's hot key principals retain the ability to vote and set followees on behalf of the new controller's neuron without the new controller's knowledge or consent.

### Finding Description
In `spawn_neuron`, the child neuron is constructed with `child_controller` set to the caller-supplied `new_controller`, but the parent's `hot_keys` are cloned verbatim into the child:

```rust
let child_neuron = NeuronBuilder::new(
    child_nid,
    to_subaccount,
    child_controller,                          // new, different principal
    ...
)
.with_hot_keys(parent_neuron.hot_keys.clone()) // ← parent's hot keys copied
.with_followees(parent_neuron.followees.clone())
...
.build();
``` [1](#0-0) 

The `Spawn` command explicitly supports a `new_controller` field for transferring the spawned neuron to a different principal: [2](#0-1) 

However, no hot-key pruning occurs when `child_controller != parent_neuron.controller()`. The same pattern exists in `split_neuron` (though there the child controller is always the caller, making it less exploitable): [3](#0-2) 

Hot keys are authorized to call `Follow` and `RegisterVote` on a neuron: [4](#0-3) 

The `AddHotKey` / `RemoveHotKey` configure operations are restricted to the controller, so the new controller cannot easily discover or remove inherited hot keys without knowing which principals were added by the previous owner. [5](#0-4) 

### Impact Explanation
An attacker who adds their alt-account as a hot key to their parent neuron, then spawns a child neuron with `new_controller` = victim (e.g., via an OTC sale of the spawned neuron's stake), retains the ability to:

- Vote on NNS proposals using the victim's neuron's voting power.
- Override the victim's followee configuration (`Follow` command).

Because NNS governance proposals can authorize treasury disbursements, parameter changes, and canister upgrades, unauthorized voting power is a governance authorization bug with real protocol impact. The victim's neuron voting power is silently co-opted.

### Likelihood Explanation
`spawn_neuron` with `new_controller` is a documented, supported operation used in OTC neuron sales and maturity-splitting workflows. Any parent neuron that has ever had hot keys set (a common operational practice) will silently propagate those hot keys to every spawned child with a different controller. The attacker entry path is a standard ingress call to `manage_neuron` with `Command::Spawn`.

### Recommendation
In `spawn_neuron`, clear the hot keys when the child controller differs from the parent controller:

```rust
let inherited_hot_keys = if child_controller == parent_neuron.controller() {
    parent_neuron.hot_keys.clone()
} else {
    vec![]
};

let child_neuron = NeuronBuilder::new(...)
    .with_hot_keys(inherited_hot_keys)
    ...
```

Apply the same fix to `split_neuron` for defense in depth, since the child controller is always the caller but the parent may have hot keys the caller did not intend to propagate.

### Proof of Concept

1. Attacker owns neuron N with hot key `alt_account` (attacker-controlled).
2. Attacker calls `manage_neuron` → `Spawn { new_controller: Some(victim), ... }`.
3. Governance creates child neuron C with `controller = victim` and `hot_keys = [alt_account]`.
4. Attacker calls `manage_neuron` from `alt_account` targeting neuron C with `Command::Follow { ... }` or `Command::RegisterVote { ... }`.
5. Governance accepts the call because `alt_account` is in `C.hot_keys`, allowing the attacker to vote with the victim's neuron's stake and dissolve-delay-weighted voting power. [6](#0-5)

### Citations

**File:** rs/nns/governance/src/governance.rs (L2241-2249)
```rust
        let child_neuron = NeuronBuilder::new(
            child_nid,
            to_subaccount,
            *caller,
            parent_neuron.dissolve_state_and_age(),
            created_timestamp_seconds,
        )
        .with_hot_keys(parent_neuron.hot_keys.clone())
        .with_followees(parent_neuron.followees.clone())
```

**File:** rs/nns/governance/src/governance.rs (L2613-2631)
```rust
    pub fn spawn_neuron(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        spawn: &manage_neuron::Spawn,
    ) -> Result<NeuronId, GovernanceError> {
        // New neurons are not allowed when the heap is too large.
        self.check_heap_can_grow()?;

        let parent_neuron = self.with_neuron(id, |neuron| neuron.clone())?;

        if parent_neuron.state(self.env.now()) == NeuronState::Spawning {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Can't perform operation on neuron: Target neuron is spawning.",
            ));
        }

        if !parent_neuron.is_controlled_by(caller) {
```

**File:** rs/nns/governance/src/governance.rs (L2651-2655)
```rust
        let child_controller = if let Some(child_controller) = &spawn.new_controller {
            *child_controller
        } else {
            parent_neuron.controller()
        };
```

**File:** rs/nns/governance/src/governance.rs (L2704-2718)
```rust
        let child_neuron = NeuronBuilder::new(
            child_nid,
            to_subaccount,
            child_controller,
            DissolveStateAndAge::DissolvingOrDissolved {
                when_dissolved_timestamp_seconds: dissolve_and_spawn_at_timestamp_seconds,
            },
            created_timestamp_seconds,
        )
        .with_spawn_at_timestamp_seconds(dissolve_and_spawn_at_timestamp_seconds)
        .with_hot_keys(parent_neuron.hot_keys.clone())
        .with_followees(parent_neuron.followees.clone())
        .with_kyc_verified(parent_neuron.kyc_verified)
        .with_maturity_e8s_equivalent(maturity_to_spawn)
        .build();
```

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L751-754)
```text
// addition, commands related to voting, i.g., [manage_neuron::Follow]
// and [manage_neuron::RegisterVote], are also available to the
// registered hot keys of the neuron.
message ManageNeuron {
```

**File:** rs/nns/governance/src/neuron/types.rs (L657-675)
```rust
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
```
