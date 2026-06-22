Let me look at the critical execution path in `perform_action` and the `spawn_neuron` authorization check more carefully.

The full execution chain is now clear. Here is the complete analysis:

---

### Title
ManageNeuron Proposal with `Command::Spawn { new_controller: attacker }` Allows NeuronManagement Followee to Redirect Victim Neuron's Maturity to Attacker-Controlled Neuron — (`rs/nns/governance/src/governance.rs`)

### Summary

A NeuronManagement-topic followee of a victim neuron can submit a `ManageNeuron` proposal embedding `Command::Spawn { new_controller: Some(attacker_principal), .. }`. `validate_manage_neuron_proposal` does not block `Spawn` for non-NFP neurons (it falls through `_ => {}`). When the proposal is adopted and executed, `perform_action` re-invokes `manage_neuron` with the **victim neuron's own controller** as the `caller`, which satisfies `spawn_neuron`'s `is_controlled_by` guard. The `new_controller` field is then accepted verbatim, creating a new neuron owned by the attacker funded entirely by the victim's maturity.

### Finding Description

**Step 1 — Proposal submission passes validation.**

`validate_manage_neuron_proposal` explicitly blocks `Disburse` and `DisburseToNeuron` for non-NFP neurons, but `Spawn` falls through the catch-all: [1](#0-0) 

`Spawn` is never matched, so the function returns `Ok(())` and the proposal is accepted.

**Step 2 — Proposal adoption.**

Only followees of the target neuron on the `NeuronManagement` topic may vote. If the attacker's neuron is the sole (or majority) followee, the proposal is immediately adopted.

**Step 3 — Execution re-uses the victim's controller as `caller`.**

`perform_action` fetches the victim neuron's controller and passes it as the `caller` to `manage_neuron`: [2](#0-1) 

**Step 4 — `spawn_neuron` authorization check is satisfied by the victim's own controller.** [3](#0-2) 

Because `caller == victim_neuron.controller()`, `is_controlled_by` returns `true` and execution continues.

**Step 5 — `new_controller` is accepted without further authorization.** [4](#0-3) 

The attacker-supplied `new_controller` is used directly. No check verifies that the entity who initiated the proposal is authorized to redirect ownership to a third party.

### Impact Explanation

The spawned neuron is created with `child_controller = attacker_principal` and is funded by the victim neuron's maturity (up to 100%). The attacker gains full ownership of a new neuron whose stake is derived from the victim's accumulated maturity. This is functionally equivalent to `DisburseToNeuron` (which is explicitly blocked for non-NFP neurons), but achieved via the unguarded `Spawn` path.

### Likelihood Explanation

The precondition — attacker's neuron being a NeuronManagement followee of the victim — is a real-world configuration. Neuron owners routinely delegate management to trusted parties or DAOs via this mechanism. Once that trust relationship exists, the attack is a single ingress call followed by a vote, with no further privileges required.

### Recommendation

Add `Command::Spawn(_)` to the non-NFP block inside `validate_manage_neuron_proposal`, mirroring the treatment of `Disburse` and `DisburseToNeuron`:

```rust
Command::Spawn(_) => {
    return Err(GovernanceError::new_with_message(
        ErrorType::NotAuthorized,
        "Cannot issue a spawn command through a proposal",
    ));
}
```

Alternatively, if spawning via proposal is intentional, restrict `new_controller` to `None` (i.e., force the spawned neuron to inherit the parent's controller) when the command arrives through the proposal path.

### Proof of Concept

State-machine test outline:

1. Create victim neuron with sufficient `maturity_e8s_equivalent` and controller `victim_principal`.
2. Create attacker neuron with controller `attacker_principal`.
3. Set attacker neuron as the sole NeuronManagement followee of victim neuron.
4. Attacker calls `manage_neuron` with `MakeProposal` wrapping `ManageNeuron { neuron_id: victim_id, command: Spawn { new_controller: Some(attacker_principal), percentage_to_spawn: Some(100), nonce: None } }`.
5. Assert proposal is accepted (no validation error).
6. Tick governance to process the adopted proposal.
7. Assert a new neuron exists whose `controller == attacker_principal` and whose `maturity_e8s_equivalent` equals the victim's original maturity.
8. Assert victim neuron's maturity is now 0.

### Citations

**File:** rs/nns/governance/src/governance.rs (L2631-2633)
```rust
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

**File:** rs/nns/governance/src/governance.rs (L4163-4168)
```rust
                    Ok(Some(ref managed_neuron_id)) => {
                        if let Ok(controller) = self.with_neuron_by_neuron_id_or_subaccount(
                            managed_neuron_id,
                            |managed_neuron| managed_neuron.controller(),
                        ) {
                            let result = self.manage_neuron(&controller, &mgmt).await;
```

**File:** rs/nns/governance/src/governance.rs (L4686-4703)
```rust
        // Only not-for-profit neurons can issue disburse/split/disburse-to-neuron
        // commands through a proposal.
        if !is_managed_neuron_not_for_profit {
            match command {
                Command::Disburse(_) => {
                    return Err(GovernanceError::new_with_message(
                        ErrorType::NotAuthorized,
                        "Cannot issue a disburse command through a proposal",
                    ));
                }
                Command::DisburseToNeuron(_) => {
                    return Err(GovernanceError::new_with_message(
                        ErrorType::NotAuthorized,
                        "Cannot issue a disburse to neuron command through a proposal",
                    ));
                }
                _ => {}
            }
```
