### Title
NNS Spawn Neuron With New Controller Inherits Parent Hot Keys, Granting Previous Owner Unauthorized Governance Access - (`rs/nns/governance/src/governance.rs`)

### Summary
When a neuron is spawned with a `new_controller` that differs from the parent's controller, the child neuron inherits all hot keys from the parent neuron. The previous owner's hot keys retain the ability to vote, follow, and join/leave the Neuron Fund on behalf of the newly spawned neuron without the new controller's knowledge or consent.

### Finding Description

In `rs/nns/governance/src/governance.rs`, the `spawn_neuron` function creates a child neuron with a potentially different controller (`child_controller`) but unconditionally copies the parent's hot keys to the child: [1](#0-0) 

```rust
let child_neuron = NeuronBuilder::new(
    child_nid,
    to_subaccount,
    child_controller,          // new controller (Bob)
    ...
)
.with_hot_keys(parent_neuron.hot_keys.clone())  // parent's hot keys (Alice's) inherited!
.with_followees(parent_neuron.followees.clone())
```

The `Spawn` message explicitly supports a `new_controller` field for transferring the spawned neuron to a different principal: [2](#0-1) 

When `new_controller` is set to a different principal (Bob), the spawned neuron has Bob as controller but retains Alice's hot keys. Hot keys are authorized to:

1. **Vote** on governance proposals on behalf of the neuron
2. **Follow** other neurons
3. **Join or leave the Neuron Fund** — which can affect the neuron's maturity through SNS swap participation [3](#0-2) [4](#0-3) 

The root cause is identical to M-22: ownership transfer (`spawn` with `new_controller`) does not clear associated privileged roles (hot keys). The new controller (Bob) may never know the inherited hot keys exist, since there is no notification mechanism.

### Impact Explanation

Alice's hot keys retain unauthorized governance influence over Bob's neuron after the spawn:

- Alice's hot keys can **vote on NNS proposals** on behalf of Bob's neuron, skewing governance outcomes without Bob's consent.
- Alice's hot keys can **join Bob's neuron to the Neuron Fund**, causing Bob's neuron's maturity to be committed to SNS swaps without Bob's approval — a form of value extraction analogous to the bribe-stealing in M-22.
- Alice's hot keys can **set following rules** on Bob's neuron, causing it to automatically vote in ways Bob did not choose.

Unlike M-22 (direct fund theft), the impact here is unauthorized governance influence and potential maturity loss via Neuron Fund manipulation. The new controller Bob can remove the hot keys, but only if he is aware they exist — there is no warning or disclosure at spawn time.

### Likelihood Explanation

This is reachable by any unprivileged ingress sender who is a neuron controller. The `Spawn` command with `new_controller` is a documented, production-accessible NNS governance operation. A realistic scenario is Alice "selling" or transferring a maturity-bearing neuron to Bob by spawning it with `new_controller = Bob`. Alice's pre-existing hot keys (e.g., a hardware wallet or delegate) silently persist on Bob's neuron. No special privileges, admin keys, or majority corruption are required.

### Recommendation

When `spawn_neuron` is called with a `new_controller` that differs from the parent's controller, the child neuron should **not** inherit the parent's hot keys. The child neuron should be initialized with an empty `hot_keys` list, allowing the new controller to add their own trusted keys. A conditional check at the point of child neuron construction is sufficient:

```rust
let hot_keys = if child_controller == parent_neuron.controller() {
    parent_neuron.hot_keys.clone()
} else {
    vec![]
};
// ...
.with_hot_keys(hot_keys)
```

### Proof of Concept

1. Alice creates an NNS neuron and adds her hardware wallet as a hot key via `AddHotKey`.
2. Alice's neuron accumulates maturity over time.
3. Alice calls `manage_neuron` with `Command::Spawn(Spawn { new_controller: Some(Bob), percentage_to_spawn: Some(100), nonce: Some(42) })`.
4. The governance canister creates a child neuron with `controller = Bob` but `hot_keys = [Alice's hardware wallet]`. [5](#0-4) 
5. Alice's hardware wallet calls `manage_neuron` targeting the child neuron with `Command::Configure(Configure { operation: Some(JoinCommunityFund(...)) })`.
6. The check `is_hotkey_or_controller` passes for Alice's hot key, and Bob's neuron is enrolled in the Neuron Fund without Bob's knowledge. [6](#0-5) 
7. Bob's neuron's maturity is now subject to SNS swap participation, reducing its value — without Bob ever consenting.

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

**File:** rs/nns/governance/src/neuron/types.rs (L778-791)
```rust
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
```
