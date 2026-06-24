### Title
Hot Keys Not Cleared When Spawning NNS Neuron with a New Controller - (File: `rs/nns/governance/src/governance.rs`)

### Summary
When `spawn_neuron` is called with a `new_controller` principal different from the parent neuron's controller, the newly created child neuron inherits all of the parent's hot keys. The new controller of the spawned neuron is not informed of these inherited hot keys, and the parent's hot key holders retain the ability to vote and follow on behalf of the new controller's neuron without their knowledge or consent — a direct governance authorization analog to the LimitOrderManager operator-not-reset-on-transfer bug.

### Finding Description

In `spawn_neuron`, the child neuron is constructed with `child_controller` as its controller (which may be a completely different principal from the parent's controller), but the parent's full hot key list is unconditionally cloned onto the child: [1](#0-0) 

The `child_controller` is resolved from `spawn.new_controller` if provided, otherwise falls back to the parent's controller: [2](#0-1) 

Hot keys in NNS governance grant the ability to vote and follow on behalf of a neuron. The authorization check `is_authorized_to_vote` passes for any principal that is either the controller **or** a hot key: [3](#0-2) 

The `ManageNeuron` proto documents that `Follow` and `RegisterVote` are available to registered hot keys: [4](#0-3) 

Additionally, hot keys can join/leave the Community Fund on behalf of the neuron (confirmed by integration tests). The new controller of the spawned neuron has no way to know about the inherited hot keys unless they explicitly query the neuron's state.

The same pattern exists in `split_neuron`, though there the child controller is always the caller (same as parent controller), so the impact is lower: [5](#0-4) 

### Impact Explanation

The parent neuron's hot key holders can exercise the child neuron's governance rights (vote, follow, join/leave Community Fund) without the new controller's knowledge or authorization. This is a **governance authorization bug**: a third party retains delegated authority over a neuron after its effective ownership has been transferred to a new principal. The new controller's voting power is silently exercised by parties they never authorized, potentially influencing NNS governance outcomes. The new controller can remove the hot keys once discovered, but may never discover them.

### Likelihood Explanation

Medium. The preconditions are realistic: (1) the parent neuron has at least one hot key set — a common operational practice for cold-key security — and (2) the parent controller spawns a child neuron to a different `new_controller`, which occurs naturally when neurons are gifted, used as rewards, or transferred as part of agreements. The `Spawn` command with `new_controller` is a documented, publicly accessible ingress call requiring only the parent controller's signature.

### Recommendation

In `spawn_neuron`, when `spawn.new_controller` is `Some(...)` and differs from the parent's controller, do not copy `parent_neuron.hot_keys` to the child neuron. The child neuron should be initialized with an empty hot key list, allowing the new controller to add their own trusted hot keys. When `new_controller` is `None` (child controller equals parent controller), copying hot keys is acceptable as the same principal retains control.

### Proof of Concept

1. Alice creates NNS neuron N with hot key `H` (e.g., a WebAuthn device key).
2. Neuron N accumulates maturity over time.
3. Alice calls `manage_neuron` → `Spawn { new_controller: Some(Bob), ... }` to gift the spawned neuron to Bob.
4. `spawn_neuron` executes at line 2704–2718: child neuron C is created with `controller = Bob` and `hot_keys = [H]` (cloned from Alice's neuron).
5. Hot key `H` (still controlled by Alice or a third party) calls `manage_neuron` on neuron C with `Follow { followees: [...] }` or `RegisterVote { ... }`.
6. The call passes the `is_authorized_to_vote` check because `H ∈ C.hot_keys`.
7. Bob's neuron votes according to `H`'s instructions. Bob has no visibility into this unless he explicitly queries `get_full_neuron` and inspects the `hot_keys` field. [6](#0-5) [7](#0-6)

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

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L747-753)
```text
// All operations that modify the state of an existing neuron are
// represented by instances of `ManageNeuron`.
//
// All commands are available to the `controller` of the neuron. In
// addition, commands related to voting, i.g., [manage_neuron::Follow]
// and [manage_neuron::RegisterVote], are also available to the
// registered hot keys of the neuron.
```
