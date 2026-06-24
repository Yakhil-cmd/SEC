### Title
Hot Keys Not Cleared on Neuron Controller Change via `spawn_neuron` - (File: `rs/nns/governance/src/governance.rs`)

### Summary
When a neuron is spawned with a different controller via `spawn_neuron`, the parent neuron's hot keys are copied verbatim to the child neuron. The new controller (Owner B) receives a neuron that still carries the old controller's (Owner A's) hot keys, allowing Owner A to continue voting and following on behalf of a neuron they no longer control.

### Finding Description
In `spawn_neuron`, the child neuron is constructed with a caller-specified `new_controller` (Owner B), but the parent's `hot_keys` are cloned directly onto the child without being cleared: [1](#0-0) 

The `NeuronBuilder` call sets `child_controller` (Owner B) as the controller, then immediately calls `.with_hot_keys(parent_neuron.hot_keys.clone())`, copying all of Owner A's registered hot keys onto the new neuron.

The `split_neuron` path has the same pattern: [2](#0-1) 

However, `split_neuron` keeps the same controller as the caller, so the hot-key inheritance is less harmful there. The critical path is `spawn_neuron` with an explicit `new_controller`.

Hot keys in NNS governance are defined to allow voting and following on behalf of a neuron: [3](#0-2) 

The `Spawn` message explicitly supports assigning a different controller: [4](#0-3) 

### Impact Explanation
Owner A (the original neuron controller) adds one or more hot keys to their neuron. Owner A then calls `spawn_neuron` with `new_controller = Owner B`. The spawned neuron is now controlled by Owner B, but Owner A's hot keys remain on it. Owner A can use those hot keys to:
- Cast votes on NNS governance proposals on behalf of Owner B's neuron, distorting governance outcomes.
- Set following rules on Owner B's neuron, redirecting its voting power to neurons of Owner A's choosing.

Owner B may be entirely unaware that Owner A's hot keys are present. The new controller must proactively enumerate and remove all inherited hot keys, which is not enforced or prompted by the protocol.

**Vulnerability class:** Governance authorization bug — access credentials (hot keys) are not tied to the owner who set them and are not invalidated when the neuron controller changes.

### Likelihood Explanation
The attack is reachable by any unprivileged ingress sender who controls a neuron with hot keys. The `spawn_neuron` endpoint is a standard, publicly accessible NNS governance operation. No privileged role, leaked key, or threshold corruption is required. The attacker only needs to be the controller of the parent neuron and to specify a `new_controller` in the `Spawn` command.

### Recommendation
In `spawn_neuron`, do not copy `parent_neuron.hot_keys` to the child neuron when a `new_controller` different from the parent's controller is specified. The child neuron should be initialized with an empty hot-key list when ownership changes, consistent with the principle that hot keys are credentials tied to the owner who registered them.

Alternatively, document explicitly that the new controller must audit and remove inherited hot keys, and add a protocol-level warning or automatic clearing step.

### Proof of Concept
1. Owner A controls neuron N and calls `manage_neuron` → `Configure` → `AddHotKey` to register their secondary principal as a hot key on N.
2. Owner A calls `manage_neuron` → `Spawn` with `new_controller = Owner B` and sufficient maturity.
3. Governance creates child neuron C with `controller = Owner B` and `hot_keys = [Owner A's key]` via `.with_hot_keys(parent_neuron.hot_keys.clone())`.
4. Owner A's secondary principal calls `manage_neuron` on neuron C → `RegisterVote`, successfully casting a vote on an NNS proposal on behalf of Owner B's neuron.
5. Owner B has no indication this occurred unless they explicitly call `get_full_neuron` and inspect the `hot_keys` field.

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

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L774-788)
```text
  // Add a new hot key that can be used to manage the neuron. This
  // provides an alternative to using the controller principal’s cold key to
  // manage the neuron, which might be onerous and difficult to keep
  // secure, especially if it is used regularly. A hot key might be a
  // WebAuthn key that is maintained inside a user device, such as a
  // smartphone.
  message AddHotKey {
    option (ic_base_types.pb.v1.tui_signed_message) = true;
    ic_base_types.pb.v1.PrincipalId new_hot_key = 1 [(ic_base_types.pb.v1.tui_signed_display_q2_2021) = true];
  }
  // Remove a hot key that has been previously assigned to the neuron.
  message RemoveHotKey {
    option (ic_base_types.pb.v1.tui_signed_message) = true;
    ic_base_types.pb.v1.PrincipalId hot_key_to_remove = 1 [(ic_base_types.pb.v1.tui_signed_display_q2_2021) = true];
  }
```

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L862-870)
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
```
