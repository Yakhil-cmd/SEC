### Title
Hot Keys Not Cleared When NNS Neuron Is Spawned to a New Controller - (File: rs/nns/governance/src/governance.rs)

### Summary
When `spawn_neuron` is called with a `new_controller` different from the parent neuron's controller, the parent's hot keys are unconditionally copied to the child neuron. This allows the original hot key holders to vote, follow, and join the community fund on behalf of the new controller's neuron without the new controller's knowledge or consent — a direct analog to the FootiumEscrow approval-persistence bug.

### Finding Description
The `spawn_neuron` function in `rs/nns/governance/src/governance.rs` creates a child neuron whose controller is set to `child_controller` (which may be a completely different principal from the parent's controller), but then copies the parent's entire `hot_keys` list verbatim to the child:

```rust
let child_neuron = NeuronBuilder::new(
    child_nid,
    to_subaccount,
    child_controller,                          // new, different controller
    ...
)
.with_hot_keys(parent_neuron.hot_keys.clone()) // ← parent's hot keys copied unchanged
.with_followees(parent_neuron.followees.clone())
...
.build();
``` [1](#0-0) 

The `Spawn` message explicitly supports assigning a different controller: [2](#0-1) 

No code path clears or filters the inherited hot keys when `new_controller` differs from the parent's controller. The child neuron is therefore born with a controller chosen by the spawner **and** with all of the spawner's hot keys still attached.

Hot key authorization is checked in `is_authorized_to_vote` / `is_hotkey_or_controller`: [3](#0-2) 

Permitted hot-key operations include: `RegisterVote`, `Follow`, `JoinCommunityFund`, `LeaveCommunityFund`, and `ChangeAutoStakeMaturity` — all of which can be exercised on the new controller's neuron by the original hot key holder.

### Impact Explanation
An unprivileged ingress sender who holds a hot key on the parent neuron can, after the spawn:

1. **Vote on NNS proposals** on behalf of the new controller's neuron, skewing governance outcomes without the new controller's consent.
2. **Change followees** for most governance topics on the new controller's neuron.
3. **Join the community fund** on behalf of the new controller's neuron, committing the new controller's future maturity to the Neurons' Fund — a financially consequential, irreversible-until-manually-undone action.

The new controller (User B) receives a neuron they believe they fully control, but the spawner's hot keys are silently present. User B has no in-protocol notification of inherited hot keys and may never discover them.

### Likelihood Explanation
- `spawn_neuron` with a distinct `new_controller` is the primary on-chain mechanism for transferring maturity-derived ICP to another party (e.g., as a reward or gift).
- A spawner who has added hot keys for convenience (e.g., a mobile key) will inadvertently transfer those authorizations to the recipient's neuron.
- The recipient (new controller) can remediate by removing hot keys, but only if they are aware of the issue; there is no protocol-level warning.
- Likelihood is **low-to-medium** for deliberate exploitation, but **medium-to-high** for unintentional authorization leakage.

### Recommendation
When `spawn_neuron` is called with a `new_controller` that differs from the parent's controller, the child neuron should be created with an **empty hot key list** rather than inheriting the parent's hot keys. If hot key inheritance is desired, it should be an explicit opt-in parameter of the `Spawn` message, not the default behavior.

### Proof of Concept
1. User A (controller of neuron N) adds hot key `H1` (a key User A controls) to neuron N.
2. User A calls `manage_neuron` → `Spawn { new_controller: Some(UserB), nonce: None, percentage_to_spawn: None }`.
3. NNS Governance creates child neuron C with `controller = UserB` and `hot_keys = [H1]`. [1](#0-0) 
4. User A (via `H1`) calls `manage_neuron` on neuron C with `RegisterVote { proposal_id, vote: Yes }`.
5. Governance accepts the call because `is_authorized_to_vote` returns `true` for `H1`. [4](#0-3) 
6. User A (via `H1`) calls `manage_neuron` on neuron C with `JoinCommunityFund {}`, committing User B's maturity to the Neurons' Fund without User B's consent.
7. User B, unaware of `H1`, never removes it; User A retains persistent governance influence over User B's neuron indefinitely.

### Citations

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
