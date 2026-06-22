### Title
Spawned Neuron Inherits Parent's Hot Keys When Controller Changes — (`rs/nns/governance/src/governance.rs`)

---

### Summary

When `spawn_neuron` is called with a `new_controller` that differs from the parent neuron's controller, the newly created child neuron inherits all of the parent's hot keys verbatim. Those hot keys were authorized by the **old** controller, not the new one. The new controller never approved them, yet they retain the ability to vote, set followees, join/leave the Neuron Fund, and refresh voting power on the child neuron.

---

### Finding Description

In `rs/nns/governance/src/governance.rs`, `spawn_neuron` builds the child neuron as follows:

```rust
let child_neuron = NeuronBuilder::new(
    child_nid,
    to_subaccount,
    child_controller,          // ← new controller (Dave)
    ...
)
.with_hot_keys(parent_neuron.hot_keys.clone())  // ← ALL of Alice's hot keys copied
.with_followees(parent_neuron.followees.clone())
...
.build();
``` [1](#0-0) 

The child neuron is assigned `child_controller` (the new owner), but receives a verbatim copy of the parent's `hot_keys` list. These hot keys were added by the **parent's** controller and were never approved by `child_controller`.

The same pattern exists in `split_neuron`: [2](#0-1) 

For `split_neuron` the controller stays the same (the caller), so the inherited hot keys are still "owned" by the same principal — less severe. But for `spawn_neuron` with `new_controller`, the controller changes while the hot keys do not, creating a direct stale-authorization scenario.

Hot keys are defined and managed via `AddHotKey` / `RemoveHotKey` configure operations, which require the **controller** to authorize: [3](#0-2) 

The `add_hot_key` function enforces that only the controller can add hot keys: [4](#0-3) 

Yet `spawn_neuron` bypasses this gate entirely by directly cloning the parent's `hot_keys` into the child without the new controller's consent.

---

### Impact Explanation

Hot keys on an NNS neuron can:
- **Vote** on NNS governance proposals (including proposals that affect the entire IC protocol)
- **Set followees** (controlling how the neuron votes automatically)
- **Join or leave the Neuron Fund**
- **Refresh voting power**

A principal (Bob) who was a hot key on Alice's parent neuron retains all of these capabilities on Dave's child neuron after `spawn_neuron` with `new_controller = Dave`. Dave never approved Bob. Bob can vote on Dave's behalf, set Dave's followees to manipulate Dave's automatic voting, or join the Neuron Fund on Dave's behalf — all without Dave's knowledge or consent.

Dave can mitigate this by calling `RemoveHotKey` for each inherited hot key, but only if he is aware of them. There is no notification mechanism.

---

### Likelihood Explanation

`spawn_neuron` with a `new_controller` is a documented, legitimate use case (e.g., a user spawning maturity rewards to a cold-storage principal or a different identity). Any neuron controller can call it via a standard ingress message. The parent neuron may legitimately have multiple hot keys (e.g., a WebAuthn device key, a hardware wallet key). When the neuron is spawned to a new controller, those hot keys silently transfer. The new controller has no way to prevent this at spawn time and may not audit the inherited hot keys.

---

### Recommendation

When `spawn_neuron` is called with a `new_controller` that differs from the parent neuron's controller, the child neuron should be created with an **empty hot keys list** rather than inheriting the parent's hot keys. The new controller can then add their own hot keys explicitly.

```rust
// Only inherit hot keys if the controller is unchanged
let child_hot_keys = if child_controller == parent_neuron.controller() {
    parent_neuron.hot_keys.clone()
} else {
    vec![]
};

let child_neuron = NeuronBuilder::new(...)
    .with_hot_keys(child_hot_keys)
    ...
``` [5](#0-4) 

---

### Proof of Concept

1. Alice controls neuron N and adds Bob and Carol as hot keys via `AddHotKey`.
2. Alice calls `spawn_neuron` on N with `new_controller = Dave` and sufficient maturity.
3. A child neuron C is created with `controller = Dave` and `hot_keys = [Bob, Carol]`.
4. Dave never approved Bob or Carol.
5. Bob calls `manage_neuron` on C with `RegisterVote` or `Follow` — this succeeds because Bob is a hot key.
6. Dave's neuron votes or follows according to Bob's instructions without Dave's consent.
7. Dave can only stop this by discovering and removing each inherited hot key individually.

The entry path is a standard ingress `manage_neuron` call, reachable by any unprivileged user who is a hot key on any neuron whose controller later spawns a child to a different principal. [6](#0-5) [7](#0-6)

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

**File:** rs/nns/governance/src/governance.rs (L2613-2618)
```rust
    pub fn spawn_neuron(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        spawn: &manage_neuron::Spawn,
    ) -> Result<NeuronId, GovernanceError> {
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
