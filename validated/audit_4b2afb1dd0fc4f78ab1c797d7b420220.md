All five code claims are confirmed by the actual source. Here is the validation output:

---

Audit Report

## Title
Deregistered Known Neuron Permanently Leaks Ballot History to Unprivileged Callers — (`rs/nns/governance/src/proposals/deregister_known_neuron.rs`)

## Summary
`DeregisterKnownNeuron::execute` calls `clear_known_neuron_data()`, which nulls `known_neuron_data` but leaves `self.visibility` at `Visibility::Public` (set earlier by `set_known_neuron_data`). The `visibility()` getter then returns `Public` for any caller, causing `get_neuron_info` to expose `recent_ballots` and `joined_community_fund_timestamp_seconds` to any unprivileged principal indefinitely after deregistration.

## Finding Description
`set_known_neuron_data` (types.rs L1168–1171) atomically sets both `self.visibility = Visibility::Public` and `self.known_neuron_data = Some(...)`. Its inverse, `clear_known_neuron_data` (types.rs L1173–1177), only sets `self.known_neuron_data = None`; its own doc comment states: *"Does NOT touch visibility. If you want to go private, call set_visibility after calling this."*

`DeregisterKnownNeuron::execute` (deregister_known_neuron.rs L50) calls only `clear_known_neuron_data()` and never follows up with `set_visibility(Private)`. After execution, `self.visibility` remains `Visibility::Public`.

The `visibility()` getter (types.rs L210–214) fast-paths to `Public` when `known_neuron_data.is_some()`, but that path is now skipped. It falls through to `self.visibility`, which is still `Visibility::Public`.

In `get_neuron_info` (types.rs L918–919), `show_full` is set to `true` whenever `visibility() == Visibility::Public`, causing `recent_ballots` and `joined_community_fund_timestamp_seconds` to be included in the response for any caller, including anonymous principals. No existing guard resets visibility on deregistration.

## Impact Explanation
Any unprivileged principal can call the public `get_neuron_info` query on a formerly-known neuron and receive its full ballot history (proposal IDs + yes/no votes) and community fund membership timestamp. This constitutes unauthorized access to neuron data that the neuron owner reasonably expects to be private after deregistration. The exposure is permanent until the neuron owner manually calls `manage_neuron` to reset visibility — an action the governance deregistration gives no indication is necessary. This matches the **High** impact class: unauthorized access to neuron data with a concrete, non-hypothetical privacy harm.

## Likelihood Explanation
The trigger is a `DeregisterKnownNeuron` governance proposal executing on mainnet — a realistic, non-adversarial event. The read is a standard unauthenticated query call requiring no special access. The window is permanent absent manual owner intervention. Any observer who tracks governance proposals can identify affected neurons and query them immediately after proposal execution.

## Recommendation
In `DeregisterKnownNeuron::execute`, reset visibility after clearing known neuron data:

```rust
neuron_store.with_neuron_mut(neuron_id, |neuron| {
    neuron.clear_known_neuron_data();
    neuron.set_visibility(Visibility::Private);
})?;
```

Alternatively, make `clear_known_neuron_data` itself reset `self.visibility = Visibility::Private`, making it symmetric with `set_known_neuron_data` and eliminating the footgun documented in its own comment.

## Proof of Concept
State-machine test:
1. Create neuron N with `recent_ballots` populated and `joined_community_fund_timestamp_seconds = Some(T)`.
2. Call `neuron.set_known_neuron_data(...)` → `visibility` becomes `Public`.
3. Execute `DeregisterKnownNeuron` proposal → calls `clear_known_neuron_data()`.
4. Assert `neuron.visibility() == Visibility::Public` (confirms the bug; `known_neuron_data` is `None` so the fast-path is skipped and `self.visibility` is returned).
5. Call `get_neuron_info(N, anonymous_principal)`.
6. Assert `response.recent_ballots` is non-empty and `response.joined_community_fund_timestamp_seconds == Some(T)`.

Both assertions pass, confirming the leak.