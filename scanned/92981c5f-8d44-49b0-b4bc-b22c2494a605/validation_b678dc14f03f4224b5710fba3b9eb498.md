The code evidence is conclusive. Let me trace the exact path:

**`set_known_neuron_data`** explicitly sets `self.visibility = Visibility::Public`: [1](#0-0) 

**`clear_known_neuron_data`** only nulls `known_neuron_data` — the comment even admits it does not touch visibility: [2](#0-1) 

**`DeregisterKnownNeuron::execute`** calls only `clear_known_neuron_data()`, never `set_visibility(Private)`: [3](#0-2) 

**`visibility()` getter** falls through to `self.visibility` when `known_neuron_data.is_none()` — which is now `Public`: [4](#0-3) 

**`get_neuron_info`** sets `show_full = true` when `visibility() == Visibility::Public`, then populates `recent_ballots` and `joined_community_fund_timestamp_seconds`: [5](#0-4) 

The path is concrete and the root cause is confirmed.

---

### Title
Deregistered Known Neuron Permanently Leaks Ballot History to Unprivileged Callers — (`rs/nns/governance/src/proposals/deregister_known_neuron.rs`)

### Summary
When a neuron is deregistered as a known neuron via governance proposal, `clear_known_neuron_data()` removes the `known_neuron_data` field but leaves `visibility` set to `Visibility::Public`. The `visibility()` getter then returns `Public` for any unprivileged caller, causing `get_neuron_info` to expose `recent_ballots` and `joined_community_fund_timestamp_seconds` indefinitely.

### Finding Description
`set_known_neuron_data` symmetrically sets both `known_neuron_data = Some(...)` and `visibility = Visibility::Public`. Its inverse, `clear_known_neuron_data`, only sets `known_neuron_data = None` — the function's own doc comment acknowledges this asymmetry: *"Does NOT touch visibility. If you want to go private, call set_visibility after calling this."*

`DeregisterKnownNeuron::execute` calls only `clear_known_neuron_data()` and never follows up with `set_visibility(Private)`. After execution, `self.visibility` remains `Visibility::Public`. The `visibility()` getter's `known_neuron_data.is_some()` fast-path is now skipped (it is `None`), so it falls through to `self.visibility`, returning `Public`.

In `get_neuron_info`, `show_full` is set to `true` whenever `visibility() == Visibility::Public`, causing `recent_ballots` and `joined_community_fund_timestamp_seconds` to be included in the response for any caller, including anonymous/unprivileged principals.

### Impact Explanation
Any unprivileged principal can call the public `get_neuron_info` query on a formerly-known neuron and receive its full ballot history (proposal IDs + yes/no votes) and community fund membership timestamp. This persists until the neuron owner manually calls `manage_neuron` to set visibility back to Private — an action they may never take because the governance deregistration gives no indication that visibility was not reset.

### Likelihood Explanation
The trigger is a governance proposal (`DeregisterKnownNeuron`) executing on mainnet — a realistic, non-privileged-attacker event. The read is a standard unauthenticated query call. No special access is required. The window is permanent (until the neuron owner manually intervenes).

### Recommendation
In `DeregisterKnownNeuron::execute`, reset visibility to `Private` after clearing known neuron data:

```rust
neuron_store.with_neuron_mut(neuron_id, |neuron| {
    neuron.clear_known_neuron_data();
    // Restore default private visibility now that known-neuron status is removed.
    neuron.set_visibility_to_private();
})?;
```

Alternatively, fix `clear_known_neuron_data` itself to reset `self.visibility = Visibility::Private`, making the operation symmetric with `set_known_neuron_data`.

### Proof of Concept
State-machine test sketch:
1. Create neuron N with `recent_ballots` populated and `joined_community_fund_timestamp_seconds = Some(...)`.
2. Call `set_known_neuron_data(...)` → `visibility` becomes `Public`.
3. Execute `DeregisterKnownNeuron` proposal → calls `clear_known_neuron_data()`.
4. Assert `neuron.visibility() == Visibility::Public` (confirms the bug).
5. Call `get_neuron_info(N, anonymous_principal)`.
6. Assert `response.recent_ballots` is non-empty and `response.joined_community_fund_timestamp_seconds` is `Some(...)`.

Both assertions will pass, confirming the leak.

### Citations

**File:** rs/nns/governance/src/neuron/types.rs (L197-215)
```rust
    pub fn visibility(&self) -> Visibility {
        // Log (and in non-release builds, also panic) if inconsistent.
        let inconsistent =
            self.known_neuron_data.is_some() && (self.visibility != Visibility::Public);
        if inconsistent {
            println!(
                "{}WARNING: Neuron is inconsistent. In release builds, it will be treated \
                 as Public. Otherwise, the next statement is a panic. Neuron: {:#?}",
                LOG_PREFIX, self,
            );
            debug_assert!(false);
        }

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

**File:** rs/nns/governance/src/neuron/types.rs (L1164-1171)
```rust
    /// In addition to what the name says, this also sets visibility to Public.
    //
    /// See also set_visibiliy, as well as the getters for known_neuron_data and
    /// visibility.
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
