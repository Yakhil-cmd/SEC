### Title
NNS `spawn_neuron` Copies Parent Hot Keys to Child Neuron With a Different Controller, Granting Unauthorized Voting Authority - (File: `rs/nns/governance/src/governance.rs`)

---

### Summary

When `spawn_neuron` is called with a `new_controller` that differs from the parent neuron's controller, the child neuron is created with the parent's full `hot_keys` list intact. Hot keys are authorized to vote and change followees on behalf of a neuron. The new controller of the child neuron does not consent to, and may not be aware of, these inherited hot keys, allowing the parent's hot key holders to exercise governance authority over a neuron they do not own.

---

### Finding Description

In `rs/nns/governance/src/governance.rs`, the `spawn_neuron` function builds the child neuron as follows:

```rust
let child_neuron = NeuronBuilder::new(
    child_nid,
    to_subaccount,
    child_controller,          // ← can be a completely different principal
    ...
)
.with_spawn_at_timestamp_seconds(...)
.with_hot_keys(parent_neuron.hot_keys.clone())  // ← parent's hot keys copied unconditionally
.with_followees(parent_neuron.followees.clone())
...
.build();
``` [1](#0-0) 

The `child_controller` is resolved from `spawn.new_controller`, which is an optional field that allows the caller to designate an entirely different principal as the owner of the spawned neuron:

```rust
let child_controller = if let Some(child_controller) = &spawn.new_controller {
    *child_controller
} else {
    parent_neuron.controller()
};
``` [2](#0-1) 

Despite the child neuron having a new, unrelated controller, the parent's `hot_keys` are cloned into the child without any conditional check or clearing. Hot keys are authorized to perform non-privileged but governance-significant operations:

```rust
pub(crate) fn is_authorized_to_vote(&self, principal: &PrincipalId) -> bool {
    self.is_hotkey_or_controller(principal)
}
fn is_hotkey_or_controller(&self, principal: &PrincipalId) -> bool {
    self.is_controlled_by(principal) || self.hot_keys.contains(principal)
}
``` [3](#0-2) 

This means any principal in the parent's `hot_keys` list retains the ability to vote and change followees on the child neuron, even though the child neuron now belongs to a completely different controller.

By contrast, `disburse_to_neuron` explicitly does **not** copy hot keys to the child neuron — the child is built fresh with only the specified controller and default followees: [4](#0-3) 

The `split_neuron` path also copies hot keys, but there the child controller is always the same as the caller (the parent's controller), so the hot key inheritance is at least consistent with the owner's intent. The `spawn_neuron` path is the only one that allows a different `new_controller` while still blindly copying hot keys.

---

### Impact Explanation

A principal holding a hot key on the parent neuron can, after the child neuron exits the spawning state (7-day delay):

1. **Vote on NNS proposals** using the child neuron's voting power without the new controller's knowledge or consent — directly manipulating governance outcomes.
2. **Change the child neuron's followees** on any topic except `NeuronManagement`, redirecting the child neuron's liquid democracy delegation.
3. **Join or leave the Neurons' Fund** on behalf of the child neuron, affecting the new controller's economic exposure.

The new controller (Bob) receives a neuron whose governance authority is partially held by principals they never authorized. Bob may not know to inspect `hot_keys` and remove them before the spawn delay expires.

---

### Likelihood Explanation

The `spawn_neuron` with a `new_controller` is a documented, reachable, and used feature — it is exercised in integration tests and exposed via the Rosetta API. Any neuron controller who has previously added hot keys (a common operational practice for cold-key security) and then spawns a neuron for a different principal triggers this condition. The hot key holder needs no special privilege beyond already being a registered hot key on the parent neuron. The 7-day spawning window gives the new controller an opportunity to remove the hot keys, but there is no notification mechanism and no enforcement that they do so.

---

### Recommendation

In `spawn_neuron`, when `spawn.new_controller` is `Some(controller)` and `controller != parent_neuron.controller()`, the child neuron should be constructed with an **empty hot keys list**:

```rust
let hot_keys_for_child = if child_controller == parent_neuron.controller() {
    parent_neuron.hot_keys.clone()
} else {
    vec![]
};
// ...
.with_hot_keys(hot_keys_for_child)
```

This mirrors the behavior of `disburse_to_neuron`, which does not propagate hot keys to the child, and is consistent with the principle that a new controller should start with a clean authorization state.

---

### Proof of Concept

1. Alice creates a neuron and adds Mallory as a hot key via `AddHotKey`.
2. Alice accumulates maturity and calls `spawn_neuron` with `new_controller = Some(Bob)` and `nonce = Some(N)`.
3. The NNS governance canister creates a child neuron with `controller = Bob` and `hot_keys = [Mallory]`.
4. After the 7-day spawn delay, the child neuron exits the spawning state and becomes eligible to vote.
5. Mallory calls `manage_neuron` → `RegisterVote` on the child neuron ID, casting a vote on an active NNS proposal using Bob's neuron.
6. Bob's voting power has been exercised by Mallory without Bob's knowledge or consent.
7. Bob can remove Mallory via `RemoveHotKey`, but only if Bob discovers the hot key exists — there is no alert or enforcement. [5](#0-4)

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

**File:** rs/nns/governance/src/governance.rs (L2613-2731)
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

        let percentage: u32 = spawn.percentage_to_spawn.unwrap_or(100);
        if percentage > 100 || percentage == 0 {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "The percentage of maturity to spawn must be a value between 1 and 100 (inclusive).",
            ));
        }

        let maturity_to_spawn = parent_neuron
            .maturity_e8s_equivalent
            .checked_mul(percentage as u64)
            .expect("Overflow while processing maturity to spawn.");
        let maturity_to_spawn = maturity_to_spawn.checked_div(100).unwrap();

        // Validate that if a child neuron controller was provided, it is a valid
        // principal.
        let child_controller = if let Some(child_controller) = &spawn.new_controller {
            *child_controller
        } else {
            parent_neuron.controller()
        };

        let economics = self
            .heap_data
            .economics
            .as_ref()
            .expect("Governance does not have NetworkEconomics")
            .clone();

        // Check if the least possible stake this neuron would be spawned with
        // is more than the minimum neuron stake.
        let least_possible_stake = (maturity_to_spawn as f64 * (1_f64 - 0.05)) as u64;

        if least_possible_stake < economics.neuron_minimum_stake_e8s {
            return Err(GovernanceError::new_with_message(
                ErrorType::InsufficientFunds,
                "There isn't enough maturity to spawn a new neuron due to worst case maturity modulation.",
            ));
        }

        let child_nid = self.neuron_store.new_neuron_id(&mut *self.randomness)?;

        // Use provided sub-account if any, otherwise generate a random one.
        let to_subaccount = match spawn.nonce {
            None => self
                .neuron_store
                .new_neuron_subaccount(&mut *self.randomness)?,
            Some(nonce_val) => {
                let to_subaccount =
                    ledger::compute_neuron_staking_subaccount(child_controller, nonce_val);
                self.neuron_store
                    .ensure_subaccount_available(to_subaccount)?
            }
        };

        let created_timestamp_seconds = self.env.now();
        let dissolve_and_spawn_at_timestamp_seconds =
            created_timestamp_seconds + economics.neuron_spawn_dissolve_delay_seconds;

        // Lock both parent and child neurons so that it cannot interleave with other async
        // operations on those neurons and spawn doesn't happen while the parent is in a corrupted
        // state.
        let in_flight_command = NeuronInFlightCommand {
            timestamp: created_timestamp_seconds,
            command: Some(InFlightCommand::SyncCommand(SyncCommand {})),
        };
        let _parent_lock = self.lock_neuron_for_command(id.id, in_flight_command.clone())?;
        let _child_lock = self.lock_neuron_for_command(child_nid.id, in_flight_command.clone())?;

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

        // `add_neuron` will verify that `child_neuron.controller` `is_self_authenticating()`, so we don't need to check it here.
        self.add_neuron(child_nid.id, child_neuron)?;

        // Get the parent neuron again, but this time mutable references.
        self.with_neuron_mut(id, |parent_neuron| {
            // Reset the parent's maturity.
            parent_neuron.maturity_e8s_equivalent -= maturity_to_spawn;
        })
        .expect("Neuron not found");

        Ok(child_nid)
    }
```

**File:** rs/nns/governance/src/neuron/types.rs (L243-255)
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
```
