Audit Report

## Title
Spawn Neuron Minimum-Stake Guard Uses Stale 5% Worst-Case While Mission 70 Modulation Reaches -10% — (`rs/nns/governance/src/governance.rs`)

## Summary
`spawn_neuron` guards against sub-minimum-stake child neurons using a hardcoded 5% worst-case reduction, but Mission 70 extended the lower modulation bound to -10%. A neuron controller whose maturity falls in the gap between these two thresholds passes the guard at spawn time, but when `maybe_spawn_neurons` later applies the real -10% modulation, the minted child neuron receives `cached_neuron_stake_e8s < neuron_minimum_stake_e8s`, violating the NNS governance economic invariant.

## Finding Description
**Guard in `spawn_neuron` (governance.rs:2666):**
```rust
let least_possible_stake = (maturity_to_spawn as f64 * (1_f64 - 0.05)) as u64;
if least_possible_stake < economics.neuron_minimum_stake_e8s {
    return Err(...);
}
```
This hardcodes 5% as the worst-case modulation. [1](#0-0) 

**Mission 70 constants define a -10% lower bound:**
```rust
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000;
pub(crate) const MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70: i64 = 200;
``` [2](#0-1) 

**`maybe_spawn_neurons` validates against the Mission 70 range and applies the real modulation:**
The sanity check at line 6438 uses `VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE`, and the log message at lines 6440–6444 explicitly references `MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70` / `MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70`, confirming the enforced range is [-1000, 200]. [3](#0-2) 

The actual minting at line 6484–6487 applies `apply_maturity_modulation(original_maturity, maturity_modulation)` with the real modulation value, which can be as low as -1000 permyriad (-10%). [4](#0-3) 

The old CMC-based system had ±5% (`MIN_MATURITY_MODULATION_PERMYRIAD = -500`). [5](#0-4)  Mission 70 extended the lower bound to -10% but the guard in `spawn_neuron` was never updated.

**Exploit path:**
1. Attacker sets `maturity_to_spawn = ceil(neuron_minimum_stake_e8s / 0.95)` (e.g., 105,263,159 for 1 ICP minimum).
2. Guard computes `105,263,159 × 0.95 = 100,000,001` → passes.
3. Neuron enters spawning state with `spawn_at_timestamp_seconds` set.
4. When modulation is at -1000 permyriad (sustained ICP price decline), `maybe_spawn_neurons` computes `105,263,159 × 0.90 = 94,736,843`.
5. Child neuron is minted with `cached_neuron_stake_e8s = 94,736,843 < 100,000,000 = neuron_minimum_stake_e8s`. [6](#0-5) 

## Impact Explanation
A child neuron is created in NNS governance with `cached_neuron_stake_e8s` below `neuron_minimum_stake_e8s`, violating the explicit economic invariant that governance enforces everywhere else. The funds are minted to the neuron's subaccount (not permanently lost), but the neuron exists in a state governance explicitly prohibits. This constitutes a concrete NNS governance invariant violation with user-level harm: the neuron may fail downstream stake checks, and normal top-up flows may not apply. This matches the allowed impact: **Significant NNS security impact with concrete user or protocol harm** (High, $2,000–$10,000).

## Likelihood Explanation
- Triggerable by any unprivileged neuron controller via standard ingress — no special privileges required.
- The exploitable maturity window is narrow but deterministically computable from public knowledge of `neuron_minimum_stake_e8s`.
- Requires modulation to reach -1000 permyriad, which depends on a sustained ICP price decline (7-day average significantly below 365-day average) — a realistic market condition, not a theoretical one.
- The attacker can monitor public modulation values and time the spawn call accordingly.

## Recommendation
Update the guard in `spawn_neuron` to use the actual Mission 70 worst-case, derived directly from the constant to stay in sync:

```rust
use crate::timer_tasks::update_icp_xdr_rate_related_data::MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70;

let worst_case_factor = 1.0 + (MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as f64 / 10_000.0);
let least_possible_stake = (maturity_to_spawn as f64 * worst_case_factor) as u64;
```

This ensures the guard and the actual modulation bounds remain in sync automatically if Mission 70 constants are ever updated.

## Proof of Concept

```rust
// State-machine test sketch
let min_stake = economics.neuron_minimum_stake_e8s; // e.g. 100_000_000
let maturity_to_spawn = (min_stake as f64 / 0.95).ceil() as u64; // 105_263_159

// Set parent neuron maturity
neuron.maturity_e8s_equivalent = maturity_to_spawn;

// Call spawn_neuron — PASSES the 5% guard
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
// INVARIANT VIOLATED: 105_263_159 * 0.90 = 94_736_843 < 100_000_000
assert!(child.cached_neuron_stake_e8s < min_stake);
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

**File:** rs/nns/governance/src/governance.rs (L6437-6447)
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
```

**File:** rs/nns/governance/src/governance.rs (L6484-6502)
```rust
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

**File:** rs/nns/governance/src/governance.rs (L6509-6521)
```rust
                    let (staked_neuron_clone, original_spawn_at_timestamp_seconds) = self
                        .with_neuron_mut(&neuron_id, |neuron| {
                            // Reset the neuron's maturity and set that it's spawning before we actually mint
                            // the stake. This is conservative to prevent a neuron having _both_ the stake and
                            // the maturity at any point in time.
                            let original_spawn_ts = neuron.spawn_at_timestamp_seconds;
                            neuron.maturity_e8s_equivalent = 0;
                            neuron.spawn_at_timestamp_seconds = None;
                            neuron.cached_neuron_stake_e8s = neuron_stake;

                            (neuron.clone(), original_spawn_ts)
                        })
                        .unwrap();
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
