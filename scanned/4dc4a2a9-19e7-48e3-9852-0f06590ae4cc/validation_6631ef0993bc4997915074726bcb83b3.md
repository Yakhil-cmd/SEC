### Title
Hot Keys Not Cleared When NNS Neuron Is Spawned to a Different Controller - (File: `rs/nns/governance/src/governance.rs`)

### Summary
When `spawn_neuron` is called with a `new_controller` different from the parent neuron's controller, the spawned child neuron inherits the parent's full `hot_keys` list. Those hot keys belong to the original owner and allow voting, following, and dissolve-state management on the new owner's neuron without the new owner's knowledge or consent.

### Finding Description
`spawn_neuron` in `rs/nns/governance/src/governance.rs` accepts an optional `new_controller` field in the `Spawn` command. When provided, the spawned neuron is created with `child_controller` set to the new principal. However, the child neuron is unconditionally built with `.with_hot_keys(parent_neuron.hot_keys.clone())`:

```rust
let child_neuron = NeuronBuilder::new(
    child_nid,
    to_subaccount,
    child_controller,          // ← can be a completely different principal
    ...
)
.with_hot_keys(parent_neuron.hot_keys.clone())  // ← parent's hot keys copied verbatim
.with_followees(parent_neuron.followees.clone())
...
.build();
``` [1](#0-0) 

The `Spawn` message explicitly documents that `new_controller` may differ from the parent's controller: [2](#0-1) 

The `child_controller` assignment path confirms the divergence: [3](#0-2) 

There is no guard that clears or filters `hot_keys` when `child_controller != parent_neuron.controller()`.

### Impact Explanation
NNS hot keys are authorized to vote on proposals and set followees on behalf of a neuron (`is_authorized_to_vote` accepts controller **or** hot key). The new controller (Bob) receives a neuron whose hot keys are entirely controlled by the spawning party (Alice). Alice's hot keys can:

1. **Vote on governance proposals** using Bob's neuron's voting power, against Bob's wishes.
2. **Change Bob's neuron's followees**, redirecting Bob's liquid-democracy delegation.
3. **Start or stop dissolving** Bob's neuron (hot keys are checked for dissolve-state operations in some paths).

Bob has no notification mechanism; he must proactively inspect `hot_keys` after receiving the neuron. The new controller cannot remove those hot keys without first discovering them, and the hot keys remain active immediately upon neuron creation. [4](#0-3) 

### Likelihood Explanation
The attack path is fully reachable by any unprivileged ingress sender who:
1. Holds a neuron with accumulated maturity and at least one hot key registered.
2. Calls `manage_neuron` → `Spawn` with `new_controller` set to a victim principal.

No privileged role, key compromise, or social engineering is required. The `spawn_neuron` method is a standard, publicly documented NNS operation exposed via the `manage_neuron` update endpoint. [5](#0-4) 

### Recommendation
When `spawn.new_controller` is set to a principal different from `parent_neuron.controller()`, the child neuron should be initialized with an **empty hot keys list** rather than inheriting the parent's hot keys. The new controller can add their own hot keys after taking ownership. Concretely, replace the unconditional `.with_hot_keys(parent_neuron.hot_keys.clone())` with a conditional:

```rust
let inherited_hot_keys = if child_controller == parent_neuron.controller() {
    parent_neuron.hot_keys.clone()
} else {
    vec![]
};
// ...
.with_hot_keys(inherited_hot_keys)
```

The same logic should be reviewed for `split_neuron`, though the impact there is lower because the child neuron always shares the same controller as the parent. [6](#0-5) 

### Proof of Concept
1. Alice controls neuron `N` with hot key `H` (e.g., a second device or a colluding principal).
2. Alice accumulates sufficient maturity on `N`.
3. Alice calls `manage_neuron` with `Command::Spawn(Spawn { new_controller: Some(Bob), nonce: Some(42), percentage_to_spawn: Some(100) })`.
4. Governance creates child neuron `C` with `controller = Bob` and `hot_keys = [H]`.
5. `H` immediately calls `manage_neuron` on `C` with `Command::Follow(...)` or `Command::RegisterVote(...)`, exercising Bob's voting power on any open proposal.
6. Bob, unaware of `H`, cannot prevent this without first querying `get_full_neuron` and then issuing a `RemoveHotKey` command — a race condition if `H` acts before Bob notices. [7](#0-6)

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

**File:** rs/nns/governance/src/governance.rs (L2613-2633)
```rust
    pub fn spawn_neuron(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        spawn: &manage_neuron::Spawn,
    ) -> Result<NeuronId, GovernanceError> {
        // New neurons are not allowed when the heap is too large.
        self.check_heap_can_grow()?;

        let parent_neuron = self.with_neuron(id, |neuron| neuron.clone())?;

        if parent_neuron.state(self.env.now()) == NeuronState::Spawning {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Can't perform operation on neuron: Target neuron is spawning.",
            ));
        }

        if !parent_neuron.is_controlled_by(caller) {
            return Err(GovernanceError::new(ErrorType::NotAuthorized));
        }
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

**File:** rs/nns/governance/src/neuron/types.rs (L254-256)
```rust
    fn is_hotkey_or_controller(&self, principal: &PrincipalId) -> bool {
        self.is_controlled_by(principal) || self.hot_keys.contains(principal)
    }
```

**File:** rs/nns/governance/canister/canister.rs (L281-286)
```rust
async fn manage_neuron(_manage_neuron: ManageNeuronRequest) -> ManageNeuronResponse {
    debug_log("manage_neuron");
    governance_mut()
        .manage_neuron(&caller(), &(gov_pb::ManageNeuron::from(_manage_neuron)))
        .await
}
```
