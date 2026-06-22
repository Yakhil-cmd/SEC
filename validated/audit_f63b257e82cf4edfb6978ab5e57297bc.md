### Title
Spawn Neuron Minimum-Stake Guard Uses Stale 5% Worst-Case While Mission 70 Modulation Reaches -10% — (`rs/nns/governance/src/governance.rs`)

### Summary

`spawn_neuron` guards against sub-minimum-stake child neurons by applying a hardcoded 5% worst-case reduction. Under Mission 70, `maybe_spawn_neurons` can apply up to -10% modulation. A neuron controller whose maturity falls in the gap between these two thresholds passes the guard but produces a child neuron minted below `neuron_minimum_stake_e8s`.

### Finding Description

**Guard in `spawn_neuron` (governance.rs:2666):**

```rust
let least_possible_stake = (maturity_to_spawn as f64 * (1_f64 - 0.05)) as u64;
if least_possible_stake < economics.neuron_minimum_stake_e8s {
    return Err(...);
}
```

This hardcodes 5% (500 permyriad) as the worst-case modulation. [1](#0-0) 

**Actual Mission 70 modulation bounds:**

```rust
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000; // -10%
pub(crate) const MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70: i64 = 200;    // +2%
``` [2](#0-1) 

**`maybe_spawn_neurons` validates against the Mission 70 range and applies the real modulation:**

```rust
if !VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE.contains(&maturity_modulation) {
    return;
}
// ...
let neuron_stake: u64 = match apply_maturity_modulation(original_maturity, maturity_modulation) { ... }
``` [3](#0-2) 

The old CMC-based system had a ±5% range (`MIN_MATURITY_MODULATION_PERMYRIAD = -500`). Mission 70 extended the lower bound to -10% (`-1_000` permyriad), but the guard in `spawn_neuron` was never updated to match. [4](#0-3) 

### Impact Explanation

For `neuron_minimum_stake_e8s = 100_000_000` (1 ICP):

- Attacker sets `maturity_to_spawn = 105_263_159` (just above `100_000_000 / 0.95`)
- Guard computes: `105_263_159 * 0.95 = 100_000_001` → **passes**
- `maybe_spawn_neurons` with `-1000` permyriad computes: `105_263_159 * 0.90 = 94_736_843` → **below minimum**

The child neuron is minted with `cached_neuron_stake_e8s < neuron_minimum_stake_e8s`, violating the economic invariant. The funds are not destroyed (they are minted to the neuron's subaccount), but the neuron exists in a state that governance explicitly prohibits. Such a neuron cannot be topped up to minimum via normal flows and may behave unexpectedly in downstream stake checks.

### Likelihood Explanation

- The attacker is an unprivileged neuron controller calling `spawn_neuron` via standard ingress — no privileged access required.
- The maturity modulation reaching -1000 permyriad requires a sustained ICP price decline (7-day average significantly below 365-day average), which is a realistic market condition.
- The exploitable maturity window is narrow but deterministically computable by the attacker given public knowledge of `neuron_minimum_stake_e8s`.
- The path is fully local-testable: set `maturity_to_spawn = ceil(min_stake / 0.95)`, set `maturity_modulation = -1000`, call `spawn_neuron`, advance time, call `maybe_spawn_neurons`, assert `cached_neuron_stake_e8s < neuron_minimum_stake_e8s`.

### Recommendation

Update the guard in `spawn_neuron` to use the actual Mission 70 worst-case:

```rust
// Use -10% (1000 permyriad) to match MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70
let least_possible_stake = (maturity_to_spawn as f64 * (1_f64 - 0.10)) as u64;
```

Or, better, derive the constant from `MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70` directly to keep the two in sync:

```rust
let worst_case_factor = 1.0 + (MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as f64 / 10_000.0);
let least_possible_stake = (maturity_to_spawn as f64 * worst_case_factor) as u64;
```

### Proof of Concept

```rust
// State-machine test sketch
let min_stake = economics.neuron_minimum_stake_e8s; // e.g. 100_000_000
let maturity_to_spawn = (min_stake as f64 / 0.95).ceil() as u64; // 105_263_159

// Set parent neuron maturity
neuron.maturity_e8s_equivalent = maturity_to_spawn;

// Call spawn_neuron — this PASSES the guard (5% check)
let child_nid = gov.spawn_neuron(&id, &caller, &Spawn { ... }).unwrap();

// Set modulation to Mission 70 minimum (-10%)
gov.heap_data.maturity_modulation = Some(MaturityModulation {
    current_value_permyriad: Some(-1000),
    updated_at_days_since_epoch: Some(now / ONE_DAY_SECONDS),
});

// Advance time past spawn_at_timestamp_seconds
driver.advance_time_by(7 * 86400);
gov.maybe_spawn_neurons().now_or_never().unwrap();

let child = gov.get_full_neuron(&child_nid, &child_controller).unwrap();
// INVARIANT VIOLATED:
assert!(child.cached_neuron_stake_e8s < min_stake);
// 105_263_159 * 0.90 = 94_736_843 < 100_000_000
```

### Citations

**File:** rs/nns/governance/src/governance.rs (L2664-2673)
```rust
        // Check if the least possible stake this neuron would be spawned with
        // is more than the minimum neuron stake.
        let least_possible_stake = (maturity_to_spawn as f64 * (1_f64 - 0.05)) as u64;

        if least_possible_stake < economics.neuron_minimum_stake_e8s {
            return Err(GovernanceError::new_with_message(
                ErrorType::InsufficientFunds,
                "There isn't enough maturity to spawn a new neuron due to worst case maturity modulation.",
            ));
        }
```

**File:** rs/nns/governance/src/governance.rs (L6437-6502)
```rust
        // Sanity check that the maturity modulation returned is within bounds.
        if !VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE.contains(&maturity_modulation) {
            println!(
                "{}Maturity modulation (in basis points) out-of-bounds. Should be in range [{}, {}], actually is: {}",
                LOG_PREFIX,
                MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70,
                MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70,
                maturity_modulation
            );
            return;
        }

        // Acquire the global "spawning" lock.
        self.heap_data.spawning_neurons = Some(true);

        // Filter all the neurons that are currently in "spawning" state.
        // Do this here to avoid having to borrow *self while we perform changes below.
        // Spawning neurons must have maturity, and no neurons in stable storage should have maturity.
        let ready_to_spawn_ids = self
            .neuron_store
            .list_ready_to_spawn_neuron_ids(now_seconds);

        // We can't alias ready_to_spawn_ids in the loop below, but the TLA model needs access to it,
        // so we clone it here.
        #[cfg(feature = "tla")]
        let mut _tla_ready_to_spawn_ids: BTreeSet<u64> =
            ready_to_spawn_ids.iter().map(|nid| nid.id).collect();

        for neuron_id in ready_to_spawn_ids {
            // Actually mint the neuron's ICP.
            let in_flight_command = NeuronInFlightCommand {
                timestamp: now_seconds,
                command: Some(InFlightCommand::Spawn(neuron_id)),
            };

            // Add the neuron to the set of neurons undergoing ledger updates.
            match self.lock_neuron_for_command(neuron_id.id, in_flight_command.clone()) {
                Ok(mut lock) => {
                    // Since we're multiplying a potentially pretty big number by up to 10500, do
                    // the calculations as u128 before converting back.
                    let neuron = self
                        .with_neuron(&neuron_id, |neuron| neuron.clone())
                        .expect("Neuron should exist, just found in list");

                    let original_maturity = neuron.maturity_e8s_equivalent;
                    let subaccount = neuron.subaccount();

                    let neuron_stake: u64 = match apply_maturity_modulation(
                        original_maturity,
                        maturity_modulation,
                    ) {
                        Ok(neuron_stake) => neuron_stake,
                        Err(err) => {
                            // Do not retain the lock so that other Neuron operations can continue.
                            // This is safe as no changes to the neuron have been made to the neuron
                            // both internally to governance and externally in ledger.
                            println!(
                                "{}Could not apply modulation to {:?} for neuron {:?} due to {:?}, skipping",
                                LOG_PREFIX,
                                neuron.maturity_e8s_equivalent,
                                neuron.id(),
                                err
                            );
                            continue;
                        }
                    };
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L46-50)
```rust
/// Lower bound for Mission 70 maturity modulation: -10% = -1000 permyriad.
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000;

/// Upper bound for Mission 70 maturity modulation: +2% = 200 permyriad.
pub(crate) const MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70: i64 = 200;
```

**File:** rs/nervous_system/governance/src/maturity_modulation/mod.rs (L3-5)
```rust
/// The maturity modulation range in basis points.
pub const MIN_MATURITY_MODULATION_PERMYRIAD: i32 = -500;
pub const MAX_MATURITY_MODULATION_PERMYRIAD: i32 = 500;
```
