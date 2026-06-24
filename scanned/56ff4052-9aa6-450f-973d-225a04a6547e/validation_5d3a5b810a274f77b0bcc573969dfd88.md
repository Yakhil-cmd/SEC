### Title
NNS Governance `calculate_merge_neurons_effect` Bypasses `max_dissolve_delay_seconds` Cap on Target Neuron - (File: rs/nns/governance/src/governance/merge_neurons.rs)

### Summary
The `calculate_merge_neurons_effect` function in NNS governance sets the target neuron's dissolve delay to `max(source_delay, target_delay)` without capping at `max_dissolve_delay_seconds()`. Every other code path that modifies a neuron's dissolve delay (e.g., `increase_dissolve_delay`) enforces this cap. The merge path bypasses it by directly constructing the `DissolveStateAndAge` struct, allowing a target neuron to end up with a dissolve delay exceeding the protocol-enforced maximum.

### Finding Description
In `calculate_merge_neurons_effect`, the new dissolve state for the target neuron is constructed as:

```rust
let target_neuron_dissolve_state_and_age = DissolveStateAndAge::NotDissolving {
    dissolve_delay_seconds: std::cmp::max(
        source.dissolve_delay_seconds,
        target.dissolve_delay_seconds,
    ),
    aging_since_timestamp_seconds: now_seconds.saturating_sub(new_target_age_seconds),
};
``` [1](#0-0) 

This directly constructs the struct with the raw `max()` of the two dissolve delays, with no call to `max_dissolve_delay_seconds()` as a cap.

By contrast, the standard `increase_dissolve_delay` path in `DissolveStateAndAge` always enforces the cap:

```rust
let new_delay_dissolve_delay_seconds = std::cmp::min(
    dissolve_delay_seconds.saturating_add(additional_dissolve_delay_seconds),
    max_dissolve_delay_seconds(),
);
``` [2](#0-1) 

The `max_dissolve_delay_seconds()` function is dynamic: it returns `MAX_DISSOLVE_DELAY_SECONDS_PRE_MISSION_70` (8 years) or `MAX_DISSOLVE_DELAY_SECONDS_POST_MISSION_70` (2 years) depending on the `is_mission_70_voting_rewards_enabled()` feature flag. [3](#0-2) 

The Mission 70 upgrade includes a `clamp_dissolve_delay_for_all_neurons_or_panic` migration that runs in `post_upgrade` to clamp existing neurons to the new 2-year maximum. However, if the `is_mission_70_voting_rewards_enabled()` flag is toggled via a governance proposal independently of an upgrade (which is the standard IC governance mechanism), the effective maximum changes but the migration does not re-run. Any neuron whose dissolve delay was increased to up to 8 years during a period when the flag was disabled (max = 8 years) would then exceed the new 2-year cap when the flag is re-enabled — without being clamped. A merge operation on such a neuron would propagate the above-cap dissolve delay to the target neuron.

### Impact Explanation
A target neuron can end up with a `dissolve_delay_seconds` exceeding `max_dissolve_delay_seconds()`. Since voting power is computed using the dissolve delay (with a bonus that saturates at `max_dissolve_delay_seconds()`), a neuron with an above-cap delay receives more voting power than the protocol allows. This inflates the attacker's governance influence and voting rewards beyond the intended ceiling, constituting a governance authorization / resource accounting bug.

### Likelihood Explanation
Low. The precondition requires a neuron to hold a dissolve delay above the current `max_dissolve_delay_seconds()`. This is possible during the Mission 70 transition: if the feature flag is toggled off (restoring the 8-year max), a user increases their neuron's delay to 8 years via the normal `IncreaseDissolveDelay` path (which is valid at that moment), and then the flag is toggled back on (reducing the max to 2 years without a new upgrade/migration). The user then merges that neuron into a target, propagating the 8-year delay. The entry path is fully unprivileged — any neuron controller can call `manage_neuron` with a `Merge` command.

### Recommendation
Apply `max_dissolve_delay_seconds()` as a cap when computing the target neuron's new dissolve delay inside `calculate_merge_neurons_effect`:

```rust
let target_neuron_dissolve_state_and_age = DissolveStateAndAge::NotDissolving {
    dissolve_delay_seconds: std::cmp::min(
        std::cmp::max(
            source.dissolve_delay_seconds,
            target.dissolve_delay_seconds,
        ),
        max_dissolve_delay_seconds(),   // <-- add this cap
    ),
    aging_since_timestamp_seconds: now_seconds.saturating_sub(new_target_age_seconds),
};
```

This mirrors the cap already enforced by `increase_dissolve_delay` and ensures the merge path cannot produce a neuron with an above-maximum dissolve delay regardless of the current flag state.

### Proof of Concept
1. Governance flag `is_mission_70_voting_rewards_enabled()` is toggled **off** (max = 8 years).
2. Attacker calls `manage_neuron` → `IncreaseDissolveDelay` on neuron A, setting its delay to 8 years. This succeeds because `increase_dissolve_delay` caps at `max_dissolve_delay_seconds()` = 8 years. [4](#0-3) 
3. Governance flag is toggled **on** (max = 2 years). No upgrade occurs, so `clamp_dissolve_delay_for_all_neurons_or_panic` does not re-run. Neuron A still has an 8-year delay. [5](#0-4) 
4. Attacker calls `manage_neuron` → `Merge` with neuron A as source and neuron B (any non-dissolving neuron) as target.
5. `calculate_merge_neurons_effect` sets the target's dissolve delay to `max(8 years, target_delay)` = 8 years, with no cap applied. [1](#0-0) 
6. `merge_neurons` applies the effect directly to the target neuron in state. [6](#0-5) 
7. Neuron B now has a dissolve delay of 8 years, exceeding the current 2-year cap, and receives inflated voting power and rewards.

### Citations

**File:** rs/nns/governance/src/governance/merge_neurons.rs (L295-301)
```rust
    let target_neuron_dissolve_state_and_age = DissolveStateAndAge::NotDissolving {
        dissolve_delay_seconds: std::cmp::max(
            source.dissolve_delay_seconds,
            target.dissolve_delay_seconds,
        ),
        aging_since_timestamp_seconds: now_seconds.saturating_sub(new_target_age_seconds),
    };
```

**File:** rs/nns/governance/src/neuron/dissolve_state_and_age.rs (L158-178)
```rust
    pub fn increase_dissolve_delay(
        self,
        now_seconds: u64,
        additional_dissolve_delay_seconds: u32,
    ) -> Self {
        // If there is no dissolve delay, this is a no-op.  Upstream validation can decide if
        // an error should be returned to the user.
        if additional_dissolve_delay_seconds == 0 {
            return self;
        }
        let additional_dissolve_delay_seconds = additional_dissolve_delay_seconds as u64;

        match self {
            Self::NotDissolving {
                dissolve_delay_seconds,
                aging_since_timestamp_seconds,
            } => {
                let new_delay_dissolve_delay_seconds = std::cmp::min(
                    dissolve_delay_seconds.saturating_add(additional_dissolve_delay_seconds),
                    max_dissolve_delay_seconds(),
                );
```

**File:** rs/nns/governance/src/governance.rs (L300-313)
```rust
// The maximum dissolve delay allowed for a neuron.
pub const MAX_DISSOLVE_DELAY_SECONDS_PRE_MISSION_70: u64 = 8 * ONE_YEAR_SECONDS;
pub const MAX_DISSOLVE_DELAY_SECONDS_POST_MISSION_70: u64 = 2 * ONE_YEAR_SECONDS;

/// Returns the maximum dissolve delay allowed for a neuron. After the flag is enabled, we can
/// replace `max_dissolve_delay_seconds()` with `MAX_DISSOLVE_DELAY_SECONDS` and set
/// `MAX_DISSOLVE_DELAY_SECONDS` to `MAX_DISSOLVE_DELAY_SECONDS_POST_MISSION_70`.
pub fn max_dissolve_delay_seconds() -> u64 {
    if is_mission_70_voting_rewards_enabled() {
        MAX_DISSOLVE_DELAY_SECONDS_POST_MISSION_70
    } else {
        MAX_DISSOLVE_DELAY_SECONDS_PRE_MISSION_70
    }
}
```

**File:** rs/nns/governance/src/governance.rs (L2504-2510)
```rust
        let target_neuron = self
            .neuron_store
            .with_neuron_mut(&effect.target_neuron_id(), |target| {
                effect.target_effect().apply(target);
                target.clone()
            })
            .expect("Expected the target neuron to exist");
```
