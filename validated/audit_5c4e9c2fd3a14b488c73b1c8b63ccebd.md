Audit Report

## Title
Hot Keys Not Cleared on `spawn_neuron` with Different Controller — (`rs/nns/governance/src/governance.rs`)

## Summary
In `spawn_neuron`, the child neuron unconditionally inherits the parent neuron's full `hot_keys` list even when `child_controller != parent_neuron.controller()`. Because hot keys are authorized to call `RegisterVote` and `Follow` on a neuron, the original owner's hot key principals retain the ability to vote and manipulate followees on the new controller's neuron without the new controller's knowledge or consent.

## Finding Description
In `spawn_neuron`, `child_controller` is resolved from `spawn.new_controller` and may differ from the parent's controller:

```rust
let child_controller = if let Some(child_controller) = &spawn.new_controller {
    *child_controller
} else {
    parent_neuron.controller()
};
```

Immediately after, the child neuron is built with an unconditional clone of the parent's hot keys:

```rust
let child_neuron = NeuronBuilder::new(
    child_nid,
    to_subaccount,
    child_controller,
    ...
)
.with_hot_keys(parent_neuron.hot_keys.clone())  // ← no controller-change check
.with_followees(parent_neuron.followees.clone())
...
.build();
```

There is no guard of the form `if child_controller == parent_neuron.controller()` before copying hot keys. The authorization check for `RegisterVote` is:

```rust
let is_neuron_authorized_to_vote =
    self.with_neuron(neuron_id, |neuron| neuron.is_authorized_to_vote(caller))?;
```

which delegates to:

```rust
pub(crate) fn is_authorized_to_vote(&self, principal: &PrincipalId) -> bool {
    self.is_hotkey_or_controller(principal)
}

fn is_hotkey_or_controller(&self, principal: &PrincipalId) -> bool {
    self.is_controlled_by(principal) || self.hot_keys.contains(principal)
}
```

The same `is_authorized_to_vote` check gates `Follow` for non-`NeuronManagement` topics. Therefore any principal in the inherited `hot_keys` list can immediately call `RegisterVote` and `Follow` on the child neuron whose controller is the victim.

## Impact Explanation
An attacker who adds an alt-account as a hot key to their parent neuron, then spawns a child neuron with `new_controller = victim`, retains the ability to vote on NNS proposals using the victim's neuron's voting power and to override the victim's followee configuration. This constitutes unauthorized access to a governance neuron and its associated voting power. The victim's neuron is silently co-opted for governance participation without their knowledge. This maps to **High ($2,000–$10,000): Unauthorized access to neurons, governance assets** — the attacker must perform meaningful per-target work (set up the OTC sale or gift scenario) but the exploit itself is a single standard ingress call once the child neuron exists.

## Likelihood Explanation
`spawn_neuron` with `new_controller` is a documented, supported operation used in OTC neuron sales and maturity-splitting workflows. Any parent neuron that has ever had hot keys set (a common operational practice) will silently propagate those hot keys to every spawned child with a different controller. The attacker entry path is a standard `manage_neuron` call with `Command::Spawn { new_controller: Some(victim), ... }`. No special privileges beyond owning a neuron with maturity and a hot key are required. The victim cannot easily detect the inherited hot keys without explicitly inspecting their new neuron's `hot_keys` field.

## Recommendation
In `spawn_neuron`, clear hot keys when the child controller differs from the parent controller:

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

Apply the same fix to `split_neuron` for defense in depth.

## Proof of Concept
1. In a unit test using the existing `governance_with_staked_neuron` harness, create parent neuron N with controller `attacker` and add `alt_account` as a hot key via `Configure::AddHotKey`.
2. Set `N.maturity_e8s_equivalent` to a sufficient value (e.g., `123_456_789`).
3. Call `gov.spawn_neuron(&id, &attacker, &Spawn { new_controller: Some(victim), ... })`.
4. Assert that the returned child neuron C has `controller = victim` and `hot_keys = [alt_account]` (currently passes — demonstrating the bug).
5. Call `gov.manage_neuron(&alt_account, &ManageNeuron { neuron_id: C, command: RegisterVote { ... } })` and assert it succeeds (currently succeeds — demonstrating exploitability).
6. Apply the fix and assert step 5 returns `NotAuthorized`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

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

**File:** rs/nns/governance/src/governance.rs (L5594-5603)
```rust
        let is_neuron_authorized_to_vote =
            self.with_neuron(neuron_id, |neuron| neuron.is_authorized_to_vote(caller))?;
        // Check that the caller is authorized, i.e., either the
        // controller or a registered hot key.
        if !is_neuron_authorized_to_vote {
            return Err(GovernanceError::new_with_message(
                ErrorType::NotAuthorized,
                "Caller is not authorized to vote for neuron.",
            ));
        }
```

**File:** rs/nns/governance/src/governance.rs (L5725-5748)
```rust
        let (is_neuron_controlled_by_caller, is_caller_authorized_to_vote) =
            self.with_neuron(id, |neuron| {
                (
                    neuron.is_controlled_by(caller),
                    neuron.is_authorized_to_vote(caller),
                )
            })?;

        // Only the controller, or a proposal (which passes the controller as the
        // caller), can change the followees for the ManageNeuron topic.
        if follow_request.topic() == Topic::NeuronManagement && !is_neuron_controlled_by_caller {
            return Err(GovernanceError::new_with_message(
                ErrorType::NotAuthorized,
                "Caller is not authorized to manage following of neuron for the ManageNeuron topic.",
            ));
        } else {
            // Check that the caller is authorized, i.e., either the
            // controller or a registered hot key.
            if !is_caller_authorized_to_vote {
                return Err(GovernanceError::new_with_message(
                    ErrorType::NotAuthorized,
                    "Caller is not authorized to manage following of neuron.",
                ));
            }
```

**File:** rs/nns/governance/src/neuron/types.rs (L243-255)
```rust
    pub(crate) fn is_authorized_to_vote(&self, principal: &PrincipalId) -> bool {
        self.is_hotkey_or_controller(principal)
    }

    /// Returns true if and only if `principal` is authorized to
    /// call simulate_manage_neuron requests on this neuron
    pub(crate) fn is_authorized_to_simulate_manage_neuron(&self, principal: &PrincipalId) -> bool {
        self.is_hotkey_or_controller(principal)
    }

    /// Returns true if and only if `principal` is either the controller or a hotkey
    fn is_hotkey_or_controller(&self, principal: &PrincipalId) -> bool {
        self.is_controlled_by(principal) || self.hot_keys.contains(principal)
```
