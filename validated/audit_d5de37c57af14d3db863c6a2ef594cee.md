Audit Report

## Title
Hot Keys Not Reset When Spawning a Neuron with a New Controller - (File: `rs/nns/governance/src/governance.rs`)

## Summary
The `spawn_neuron` function in the NNS Governance canister unconditionally copies the parent neuron's `hot_keys` to the child neuron, even when the child is assigned a different controller via `spawn.new_controller`. This allows the parent's hot key holders to retain voting authority over the child neuron without the new controller's knowledge or consent.

## Finding Description
In `spawn_neuron`, the child controller is resolved at lines 2651–2655: if `spawn.new_controller` is provided, it is used directly; otherwise the parent's controller is used. [1](#0-0) 

Regardless of whether `child_controller` differs from the parent's controller, the child neuron is built at lines 2704–2718 with `.with_hot_keys(parent_neuron.hot_keys.clone())` — no conditional reset. [2](#0-1) 

The `register_vote` handler at lines 5594–5603 authorizes callers via `is_authorized_to_vote`, which delegates to `is_hotkey_or_controller`. [3](#0-2) 

`is_hotkey_or_controller` returns `true` if the caller is in `self.hot_keys`, regardless of who the controller is. [4](#0-3) 

There is no post-spawn cleanup or guard that strips inherited hot keys when ownership changes. The `split_neuron` path at lines 2241–2257 is less impactful because the child controller is always `*caller` (the same person who set the hot keys). [5](#0-4) 

## Impact Explanation
This is an unauthorized governance access bug. A hot key holder on the parent neuron can cast votes using the child neuron's voting power after it has been transferred to a new controller who never authorized them. This constitutes **unauthorized access to neurons and governance assets** — a High severity impact ($2,000–$10,000) per the ICP bounty scope. The new controller's voting power can be silently exercised by a third party, undermining the integrity of NNS governance participation.

## Likelihood Explanation
The `spawn` command is callable by any neuron controller via the standard `manage_neuron` ingress message — no privileged role is required. The attacker needs only: (1) a neuron with maturity, (2) a hot key registered on that neuron (their own alternate principal suffices), and (3) a target principal to assign as `new_controller`. This is a realistic scenario in neuron marketplaces or maturity-transfer arrangements. The attack is repeatable and requires no victim mistake beyond accepting a spawned neuron.

## Recommendation
In `spawn_neuron`, clear `hot_keys` on the child neuron when `child_controller` differs from the parent's controller:

```rust
.with_hot_keys(if child_controller == parent_neuron.controller() {
    parent_neuron.hot_keys.clone()
} else {
    vec![]
})
```

This change should be applied at line 2714 in `rs/nns/governance/src/governance.rs`. [6](#0-5) 

## Proof of Concept
1. Alice controls neuron N with maturity. Alice registers principal `BOB` as a hot key on N.
2. Alice calls `manage_neuron` → `Spawn { new_controller: Some(CAROL), nonce: None, percentage_to_spawn: 100 }`.
3. Governance creates child neuron C with `controller = CAROL` and `hot_keys = [BOB]` (confirmed at line 2707 and 2714). [7](#0-6) 
4. BOB calls `manage_neuron` → `RegisterVote { neuron_id: C, ... }`. The check at line 5594–5595 calls `neuron.is_authorized_to_vote(&BOB)`, which calls `is_hotkey_or_controller`, which returns `true` because `BOB ∈ hot_keys`. [8](#0-7) 
5. BOB successfully casts a vote using CAROL's neuron. CAROL never authorized BOB.

A deterministic integration test using PocketIC can reproduce this by: creating a neuron, adding a hot key, spawning to a new controller, then asserting that `register_vote` from the hot key principal succeeds on the child neuron.

### Citations

**File:** rs/nns/governance/src/governance.rs (L2241-2257)
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
        .with_kyc_verified(parent_neuron.kyc_verified)
        .with_auto_stake_maturity(parent_neuron.auto_stake_maturity.unwrap_or(false))
        .with_not_for_profit(parent_neuron.not_for_profit)
        .with_joined_community_fund_timestamp_seconds(
            parent_neuron.joined_community_fund_timestamp_seconds,
        )
        .with_neuron_type(parent_neuron.neuron_type)
        .build();
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

**File:** rs/nns/governance/src/neuron/types.rs (L243-256)
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
    }
```
