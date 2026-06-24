### Title
Spawned NNS Neuron Inherits Parent's Hot Keys When Assigned a New Controller, Granting Previous Owner's Delegates Unauthorized Voting Power — (`rs/nns/governance/src/governance.rs`)

### Summary

When `spawn_neuron` is called with a `new_controller` that differs from the parent neuron's controller, the newly created child neuron unconditionally inherits the parent's `hot_keys`. These hot keys were set by the original controller (Alice) and are now attached to a neuron whose controller is a different principal (Bob). Bob did not set these hot keys, yet they remain active and can be used to vote and change followees on Bob's neuron without Bob's knowledge or consent.

### Finding Description

In `rs/nns/governance/src/governance.rs`, the `spawn_neuron` function allows a neuron controller to spawn a child neuron from maturity and optionally assign it to a different principal via `spawn.new_controller`. [1](#0-0) 

When a `new_controller` is provided, the child neuron is built with `child_controller` as its controller, but the parent's `hot_keys` are cloned directly onto the child: [2](#0-1) 

The `hot_keys` field on the child neuron was set by Alice (the parent controller) for Alice's own neuron. After spawning, Bob controls the child neuron, but Alice's hot keys remain on it. No check is performed to ensure the hot keys were set by the current controller of the child neuron.

This contrasts with `disburse_to_neuron`, which explicitly resets followees to the system defaults when a new controller is assigned, demonstrating that the developers are aware of the ownership-transfer concern in at least one code path: [3](#0-2) 

Hot keys in NNS governance are authorized to vote and change followees (for non-`ManageNeuron` topics), as checked by `is_authorized_to_vote`: [4](#0-3) 

The `follow` function enforces that only the controller or an authorized hot key can change followees: [5](#0-4) 

### Impact Explanation

Any principal listed in the parent neuron's `hot_keys` can, after the spawn:

1. **Vote on NNS proposals** using the child neuron's voting power — without Bob's knowledge or consent.
2. **Change followees** on the child neuron for all topics except `NeuronManagement` — redirecting the child neuron's automatic voting to arbitrary followees.

This constitutes a governance authorization bypass: principals authorized by the previous controller (Alice) retain the ability to exercise governance rights over a neuron now controlled by a different principal (Bob). In scenarios where neurons are spawned for beneficiaries (vesting schedules, protocol rewards, gifts), the beneficiary's voting power can be silently hijacked by the spawner's delegates.

### Likelihood Explanation

`spawn_neuron` with `new_controller` is a standard, publicly reachable NNS governance operation callable by any neuron controller via ingress. Realistic scenarios include:

- Protocols that distribute maturity-derived neurons to users.
- Vesting arrangements where a custodian spawns neurons for beneficiaries.
- Any user gifting a spawned neuron to another principal.

The recipient (Bob) has no on-chain notification that foreign hot keys are present. Bob must proactively inspect the neuron's hot key list and call `RemoveHotKey` to remediate — a step most users will not take.

### Recommendation

In `spawn_neuron`, when `new_controller` differs from the parent neuron's controller, do not inherit the parent's `hot_keys`. The child neuron should be created with an empty hot key list (or only keys explicitly provided by the caller for the child), mirroring the behavior of `disburse_to_neuron` which resets followees to system defaults when a new controller is assigned.

```rust
// Only inherit hot_keys if the child controller is the same as the parent controller.
let child_hot_keys = if child_controller == parent_neuron.controller() {
    parent_neuron.hot_keys.clone()
} else {
    vec![]
};
```

### Proof of Concept

1. Alice controls neuron N with `hot_keys = [mallory_key]` and sufficient maturity.
2. Alice calls `manage_neuron` → `Spawn { new_controller: Some(bob), nonce: Some(1), percentage_to_spawn: Some(100) }`.
3. `spawn_neuron` creates child neuron C with `controller = bob` and `hot_keys = [mallory_key]` (inherited from N).
4. Mallory (holder of `mallory_key`) calls `manage_neuron` on neuron C with `Follow { topic: NetworkEconomics, followees: [mallory_neuron] }` — this succeeds because `is_authorized_to_vote` returns `true` for hot keys.
5. Mallory calls `manage_neuron` on neuron C with `RegisterVote { proposal_id: X, vote: Yes }` — this also succeeds.
6. Bob's neuron votes on proposals and follows Mallory's neuron without Bob's knowledge or consent. [2](#0-1) [5](#0-4)

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

**File:** rs/nns/governance/src/governance.rs (L3012-3021)
```rust
        let child_neuron = NeuronBuilder::new(
            child_nid,
            to_subaccount,
            child_controller,
            dissolve_state_and_age,
            created_timestamp_seconds,
        )
        .with_followees(self.heap_data.default_followees.clone())
        .with_kyc_verified(parent_neuron.kyc_verified)
        .build();
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

**File:** rs/nns/governance/src/neuron/types.rs (L254-256)
```rust
    fn is_hotkey_or_controller(&self, principal: &PrincipalId) -> bool {
        self.is_controlled_by(principal) || self.hot_keys.contains(principal)
    }
```
