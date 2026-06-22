### Title
Hot Keys Not Cleared on NNS Neuron Spawn with New Controller - (`rs/nns/governance/src/governance.rs`)

### Summary

When a neuron is spawned via `spawn_neuron` with a `new_controller` different from the parent's controller, the child neuron inherits the parent's full `hot_keys` list. Those hot keys retain the ability to vote and follow on behalf of the new child neuron without the new controller's consent, mirroring the "collector role not relinquished during transfer" pattern from the external report.

### Finding Description

In `spawn_neuron`, when a caller specifies a `new_controller` (a different principal), the child neuron is built with the parent's hot keys copied verbatim:

```rust
let child_neuron = NeuronBuilder::new(
    child_nid,
    to_subaccount,
    child_controller,          // ← new controller
    ...
)
.with_hot_keys(parent_neuron.hot_keys.clone())  // ← parent's hot keys copied
.with_followees(parent_neuron.followees.clone())
...
.build();
``` [1](#0-0) 

Hot keys on an NNS neuron are authorized to:
- Vote on proposals (`is_authorized_to_vote` returns true for hot keys)
- Follow other neurons
- Join/Leave the Neuron Fund [2](#0-1) 

The same pattern exists in `split_neuron`, where the child neuron also inherits the parent's hot keys: [3](#0-2) 

The `is_authorized_to_configure_or_err` function confirms hot keys can perform `JoinCommunityFund`/`LeaveCommunityFund` operations: [4](#0-3) 

### Impact Explanation

When a neuron owner spawns a child neuron to a new controller (e.g., selling or gifting maturity), the previous owner's hot keys remain on the child neuron. Those hot keys can:

1. **Vote on governance proposals** on behalf of the new owner's neuron, potentially influencing NNS governance outcomes without the new controller's knowledge or consent.
2. **Change followees** of the child neuron, redirecting its voting power.
3. **Join the Neuron Fund** on behalf of the child neuron, committing the new owner's maturity to the Neuron Fund without consent.

This is a governance authorization bug: a principal that no longer has any legitimate claim to a neuron retains partial control over it after a controller transfer via `spawn_neuron`.

### Likelihood Explanation

The `Spawn` command explicitly supports `new_controller` to transfer maturity to a different principal. This is a documented, reachable, unprivileged ingress path — any neuron controller can call `manage_neuron` with `Command::Spawn { new_controller: Some(other_principal) }`. The parent's hot keys are always copied unconditionally. Any user who has previously added hot keys to their neuron and then spawns to a new controller triggers this condition. [5](#0-4) 

### Recommendation

When `spawn_neuron` is called with a `new_controller` that differs from the parent's controller, the child neuron should be created with an **empty hot keys list** rather than inheriting the parent's hot keys. The new controller can then add their own hot keys. The same fix should be applied to `split_neuron` if the intent is to allow splitting to a different controller in the future.

```rust
// Only inherit hot keys if the child controller is the same as the parent
let child_hot_keys = if child_controller == parent_neuron.controller() {
    parent_neuron.hot_keys.clone()
} else {
    vec![]
};
```

### Proof of Concept

1. Alice controls neuron N with hot key H (e.g., a web app key).
2. Alice calls `manage_neuron` → `Spawn { new_controller: Some(Bob), ... }`.
3. Child neuron C is created with `controller = Bob` but `hot_keys = [H]`.
4. Alice (via hot key H) calls `manage_neuron` on C with `Command::Configure(JoinCommunityFund)` — this succeeds because `is_authorized_to_configure_or_err` allows hot keys for `JoinCommunityFund`.
5. Alice (via H) also calls `manage_neuron` on C with `Command::Follow(...)` — this succeeds because `is_authorized_to_vote` returns true for hot keys.
6. Bob (the new controller) has no way to detect or prevent this without explicitly auditing and removing all inherited hot keys.

Entry path: unprivileged ingress `manage_neuron` call to the NNS Governance canister — no privileged access required. [6](#0-5)

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

**File:** rs/nns/governance/src/neuron/types.rs (L239-256)
```rust
    /// Returns true if and only if `principal` is authorized to
    /// perform non-privileged operations, like vote and follow,
    /// on behalf of this neuron, i.e., if `principal` is either the
    /// controller or one of the authorized hot keys.
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

**File:** rs/nns/governance/src/neuron/types.rs (L771-807)
```rust
    fn is_authorized_to_configure_or_err(
        &self,
        caller: &PrincipalId,
        configure: &Operation,
    ) -> Result<(), GovernanceError> {
        use Operation::{JoinCommunityFund, LeaveCommunityFund};

        match configure {
            // The controller and hotkeys are allowed to change Neuron Fund membership.
            JoinCommunityFund(_) | LeaveCommunityFund(_) => {
                if self.is_hotkey_or_controller(caller) {
                    Ok(())
                } else {
                    Err(GovernanceError::new_with_message(
                        ErrorType::NotAuthorized,
                        format!(
                            "Caller '{caller:?}' must be the controller or hotkey of the neuron to join or leave the neuron fund.",
                        ),
                    ))
                }
            }

            // Only the controller is allowed to perform other configure operations.
            _ => {
                if self.is_controlled_by(caller) {
                    Ok(())
                } else {
                    Err(GovernanceError::new_with_message(
                        ErrorType::NotAuthorized,
                        format!(
                            "Caller '{caller:?}' must be the controller of the neuron to perform this operation:\n{configure:#?}",
                        ),
                    ))
                }
            }
        }
    }
```
