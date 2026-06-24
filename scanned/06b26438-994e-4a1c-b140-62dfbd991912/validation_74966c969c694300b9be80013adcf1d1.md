### Title
NNS Governance `spawn_neuron` Copies Parent Hot Keys to Child Neuron Assigned to a Different Controller — (`rs/nns/governance/src/governance.rs`)

---

### Summary

When `spawn_neuron` is called with a `new_controller` that differs from the parent neuron's controller, the spawned child neuron inherits all of the parent's `hot_keys` verbatim. The new controller receives a neuron that already has pre-authorized hot key principals they did not choose, giving the parent's hot key holders the ability to vote and follow on behalf of the new controller's neuron without their knowledge or consent.

---

### Finding Description

`spawn_neuron` in `rs/nns/governance/src/governance.rs` supports an optional `new_controller` field that lets the parent neuron's controller assign the spawned neuron to a completely different principal: [1](#0-0) 

The child neuron is then constructed with `child_controller` as its controller, but the parent's full `hot_keys` list is cloned directly onto it: [2](#0-1) 

There is no step that clears or filters the inherited hot keys when `child_controller != parent_neuron.controller()`. The new controller (Charlie) receives a neuron that already has the parent's hot key principals (Bob) authorized on it.

The same pattern exists in `split_neuron`, but there the child always shares the same controller as the caller, so the controller can immediately clean up. In `spawn_neuron` the controller is a third party who may be unaware of the pre-existing hot keys. [3](#0-2) 

In NNS governance, hot keys are authorized to:
- Vote (`RegisterVote`)
- Follow (`Follow`)
- Join/leave the Neuron Fund (`JoinCommunityFund` / `LeaveCommunityFund`) [4](#0-3) [5](#0-4) 

The `Spawn` message definition confirms `new_controller` is an optional field intended to assign the spawned neuron to a different principal: [6](#0-5) 

---

### Impact Explanation

**Impact: Medium** — governance authorization bug.

A hot key holder (Bob) who was authorized on the parent neuron retains the ability to vote and follow on behalf of the child neuron after it has been transferred to a new controller (Charlie). Bob can:

1. Cast votes on NNS proposals on Charlie's behalf, overriding or diluting Charlie's intended governance participation.
2. Set follow relationships on Charlie's neuron, causing it to automatically vote in ways Charlie did not choose.
3. Join or leave the Neuron Fund on Charlie's behalf, affecting Charlie's maturity exposure.

Charlie cannot prevent this without first discovering the inherited hot keys and removing them. Until Charlie does so, Bob has persistent unauthorized governance access. This does not allow direct fund theft (hot keys cannot disburse, split, or spawn), but it constitutes a meaningful governance authorization bypass.

---

### Likelihood Explanation

**Likelihood: Medium.**

The scenario requires:
1. Alice (parent controller) to have added at least one hot key (Bob) to her neuron before spawning.
2. Alice to spawn a neuron for a different principal (Charlie) using `new_controller`.

Both steps are normal, documented operations. The `Spawn` command explicitly supports `new_controller` for the purpose of gifting maturity to another party. A user receiving a spawned neuron from a third party has no reason to expect pre-existing hot keys and no UI-level warning exists. The attacker (Bob) needs no special access beyond having been a hot key on the parent neuron at any point before the spawn.

---

### Recommendation

In `spawn_neuron`, when `child_controller` differs from `parent_neuron.controller()`, do not copy the parent's `hot_keys` to the child neuron. The child neuron should be created with an empty hot key list, or at most only the hot keys that the new controller explicitly consents to. The fix is localized to the `NeuronBuilder` call in `spawn_neuron`:

```rust
// Only inherit hot_keys if the child controller is the same as the parent controller.
let hot_keys = if child_controller == parent_neuron.controller() {
    parent_neuron.hot_keys.clone()
} else {
    vec![]
};
``` [2](#0-1) 

---

### Proof of Concept

**Setup:**
- Alice controls neuron N with hot key Bob.
- Alice calls `manage_neuron` → `Spawn { new_controller: Some(Charlie), nonce: Some(42), percentage_to_spawn: Some(100) }`.

**What happens in `spawn_neuron`:**

1. `child_controller = Charlie` (line 2651–2655).
2. Child neuron is built with `controller = Charlie` but `.with_hot_keys(parent_neuron.hot_keys.clone())` copies `[Bob]` onto the child (line 2714).
3. Child neuron is stored in governance state with `controller = Charlie`, `hot_keys = [Bob]`.

**Exploit:**
- Bob sends `manage_neuron` → `RegisterVote { proposal: P, vote: Yes }` authenticated as Bob, targeting the child neuron.
- Governance checks `is_authorized_to_vote` → `is_hotkey_or_controller` → Bob is in `hot_keys` → **authorized**.
- Bob's vote is cast on behalf of Charlie's neuron without Charlie's knowledge. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/nns/governance/src/governance.rs (L2248-2248)
```rust
        .with_hot_keys(parent_neuron.hot_keys.clone())
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

**File:** rs/nns/governance/src/neuron/types.rs (L239-255)
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
```

**File:** rs/nns/governance/src/neuron/types.rs (L771-806)
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
```

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L862-871)
```text
  message Spawn {
    option (ic_base_types.pb.v1.tui_signed_message) = true;
    // If not set, the spawned neuron will have the same controller as
    // this neuron.
    ic_base_types.pb.v1.PrincipalId new_controller = 1 [(ic_base_types.pb.v1.tui_signed_display_q2_2021) = true];
    // The nonce with which to create the subaccount.
    optional uint64 nonce = 2;
    // The percentage to spawn, from 1 to 100 (inclusive).
    optional uint32 percentage_to_spawn = 3;
  }
```
