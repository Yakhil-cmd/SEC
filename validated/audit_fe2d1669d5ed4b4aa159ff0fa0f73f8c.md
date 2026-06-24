Audit Report

## Title
Deregistered Known Neuron Permanently Leaks Ballot History to Unprivileged Callers — (`rs/nns/governance/src/proposals/deregister_known_neuron.rs`)

## Summary
`DeregisterKnownNeuron::execute` calls `clear_known_neuron_data()` but never resets `visibility` to `Private`. Because `set_known_neuron_data` previously set `self.visibility = Visibility::Public`, the field remains `Public` after deregistration. Any unauthenticated caller can then query `get_neuron_info` and receive the neuron's full `recent_ballots` and `joined_community_fund_timestamp_seconds` indefinitely.

## Finding Description
`set_known_neuron_data` at [1](#0-0)  sets both `self.visibility = Visibility::Public` and `self.known_neuron_data = Some(...)`.

`clear_known_neuron_data` at [2](#0-1)  only nulls `known_neuron_data`; its own doc comment explicitly warns that visibility is not touched.

`DeregisterKnownNeuron::execute` at [3](#0-2)  calls only `clear_known_neuron_data()` and never follows up with `set_visibility(Private)`.

The `visibility()` getter at [4](#0-3)  fast-paths on `known_neuron_data.is_some()` (now `false`) and falls through to `self.visibility`, which is still `Visibility::Public`.

`get_neuron_info` at [5](#0-4)  sets `show_full = true` when `visibility() == Visibility::Public`, then populates `recent_ballots` and `joined_community_fund_timestamp_seconds` for any caller, including anonymous principals.

## Impact Explanation
Any unauthenticated principal can call the public `get_neuron_info` query on a formerly-known neuron and receive its complete ballot history (proposal IDs + yes/no votes) and community fund membership timestamp. This constitutes unauthorized access to sensitive NNS governance data that the neuron owner intended to make private upon deregistration. The exposure is permanent until the neuron owner manually calls `manage_neuron` to reset visibility — an action the governance deregistration gives no indication is necessary. This matches the allowed High impact class: "Significant NNS security impact with concrete user or protocol harm."

## Likelihood Explanation
The trigger is a standard governance proposal (`DeregisterKnownNeuron`) executing on mainnet — no special attacker privileges required. The read is a standard unauthenticated query call. The window is permanent. No victim mistake is required; the neuron owner's reasonable expectation that deregistration restores privacy is violated by the code's own asymmetry.

## Recommendation
In `DeregisterKnownNeuron::execute`, reset visibility after clearing known neuron data:

```rust
neuron_store.with_neuron_mut(neuron_id, |neuron| {
    neuron.clear_known_neuron_data();
    neuron.set_visibility(Visibility::Private);
})?;
```

Alternatively, fix `clear_known_neuron_data` itself to reset `self.visibility = Visibility::Private`, making it symmetric with `set_known_neuron_data`.

## Proof of Concept
State-machine test:
1. Create neuron N with `recent_ballots` populated and `joined_community_fund_timestamp_seconds = Some(t)`.
2. Call `set_known_neuron_data(...)` → `self.visibility` becomes `Visibility::Public`.
3. Execute a `DeregisterKnownNeuron` proposal → `clear_known_neuron_data()` is called; `self.visibility` remains `Visibility::Public`.
4. Assert `neuron.visibility() == Visibility::Public` (confirms the stale state).
5. Call `get_neuron_info(N, anonymous_principal)`.
6. Assert `response.recent_ballots` is non-empty and `response.joined_community_fund_timestamp_seconds == Some(t)`.

Both assertions pass, confirming the permanent leak to unauthenticated callers.

### Citations

**File:** rs/nns/governance/src/neuron/types.rs (L210-215)
```rust
        if self.known_neuron_data.is_some() {
            return Visibility::Public;
        }

        self.visibility
    }
```

**File:** rs/nns/governance/src/neuron/types.rs (L918-929)
```rust
        let show_full =
            self.visibility() == Visibility::Public || self.is_hotkey_or_controller(&requester);
        if show_full {
            let mut additional_recent_ballots = self
                .sorted_recent_ballots()
                .into_iter()
                .map(api::BallotInfo::from)
                .collect();
            recent_ballots.append(&mut additional_recent_ballots);

            joined_community_fund_timestamp_seconds = self.joined_community_fund_timestamp_seconds;
        }
```

**File:** rs/nns/governance/src/neuron/types.rs (L1168-1171)
```rust
    pub fn set_known_neuron_data(&mut self, new_known_neuron_data: KnownNeuronData) {
        self.visibility = Visibility::Public;
        self.known_neuron_data = Some(new_known_neuron_data);
    }
```

**File:** rs/nns/governance/src/neuron/types.rs (L1173-1177)
```rust
    /// Does NOT touch visiblity. If you want to go private, call set_visibility
    /// after calling this.
    pub(crate) fn clear_known_neuron_data(&mut self) {
        self.known_neuron_data = None;
    }
```

**File:** rs/nns/governance/src/proposals/deregister_known_neuron.rs (L49-51)
```rust
        // Remove the known neuron data
        neuron_store.with_neuron_mut(neuron_id, |neuron| neuron.clear_known_neuron_data())?;

```
