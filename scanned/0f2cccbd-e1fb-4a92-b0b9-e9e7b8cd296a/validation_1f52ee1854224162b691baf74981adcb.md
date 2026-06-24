### Title
NNS `spawn_neuron` Minimum Stake Guard Uses Hardcoded 5% Worst-Case Modulation While Mission 70 Allows −10% - (File: `rs/nns/governance/src/governance.rs`)

### Summary
`Governance::spawn_neuron` validates that the spawned neuron will meet `neuron_minimum_stake_e8s` by applying a hardcoded 5% worst-case modulation. However, the Mission 70 maturity-modulation system (`heap_data.maturity_modulation`) that `maybe_spawn_neurons` actually uses at settlement time has a lower bound of −10%. A user can therefore initiate a spawn whose pre-flight check passes but whose 7-day-later settlement mints a stake below the protocol minimum, violating the neuron-stake invariant.

### Finding Description

**Phase 1 – spawn request (pre-flight check):**

`spawn_neuron` computes the worst-case stake with a literal `0.05` constant:

```rust
// rs/nns/governance/src/governance.rs  line 2666
let least_possible_stake = (maturity_to_spawn as f64 * (1_f64 - 0.05)) as u64;
if least_possible_stake < economics.neuron_minimum_stake_e8s {
    return Err(GovernanceError::new_with_message(
        ErrorType::InsufficientFunds,
        "There isn't enough maturity to spawn a new neuron due to worst case maturity modulation.",
    ));
}
```

This constant was correct when the only modulation source was the CMC, whose range is `[MIN_MATURITY_MODULATION_PERMYRIAD, MAX_MATURITY_MODULATION_PERMYRIAD]` = `[−500, +500]` permyriad = ±5%.

**Phase 2 – settlement (7 days later, `maybe_spawn_neurons`):**

`maybe_spawn_neurons` reads the Mission 70 modulation from `heap_data.maturity_modulation.current_value_permyriad` and validates it against `VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE`:

```rust
// rs/nns/governance/src/governance.rs  line 276-278
const VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE: RangeInclusive<i32> =
    MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as i32
        ..=MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70 as i32;
```

Where the Mission 70 bounds are:

```rust
// rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs  lines 47-50
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000;  // −10 %
pub(crate) const MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70: i64 =    200;  // +2 %
```

The actual minting call applies whatever value is in `heap_data.maturity_modulation`:

```rust
// rs/nns/governance/src/governance.rs  line 6484-6487
let neuron_stake: u64 = match apply_maturity_modulation(
    original_maturity,
    maturity_modulation,   // ← Mission 70 value, can be −1000 permyriad
) {
```

**The gap:** the pre-flight guard assumes worst-case = −5%, but settlement can apply −10%. Any maturity value M satisfying

```
M × 0.95 ≥ neuron_minimum_stake_e8s   (guard passes)
M × 0.90 < neuron_minimum_stake_e8s   (settlement mints below minimum)
```

passes the guard and later produces a sub-minimum neuron.

### Impact Explanation

An unprivileged NNS neuron holder can call `spawn_neuron` with a maturity amount in the window `[min_stake / 0.95, min_stake / 0.90)`. The pre-flight check succeeds, the maturity is irrevocably moved from the parent neuron to the child neuron in spawning state, and 7 days later `maybe_spawn_neurons` mints a stake below `neuron_minimum_stake_e8s`. This violates the protocol invariant that every neuron must hold at least the minimum stake, and it does so without any privileged access — any neuron controller can trigger it once Mission 70 modulation reaches −10%.

### Likelihood Explanation

Mission 70 modulation starts at 0 and is speed-limited to 30 permyriad per day (`MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD`). Reaching −10% requires roughly 33 consecutive days of ICP price decline relative to the 365-day average — a realistic bear-market scenario. Once the modulation is at or near −10%, the window for exploitation is open to any neuron holder with maturity in the vulnerable range.

### Recommendation

Replace the hardcoded `0.05` constant in `spawn_neuron` with the actual Mission 70 lower bound so the pre-flight check is consistent with what `maybe_spawn_neurons` will apply:

```rust
// Use the real worst-case bound instead of the hardcoded 5 %
use ic_nervous_system_governance::maturity_modulation::apply_maturity_modulation;
let least_possible_stake = apply_maturity_modulation(
    maturity_to_spawn,
    MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as i32,
).unwrap_or(0);
```

### Proof of Concept

1. Advance the Mission 70 modulation to −10% (33+ days of price decline, or set `heap_data.maturity_modulation.current_value_permyriad = Some(-1000)` directly in a test).
2. Call `spawn_neuron` with `maturity_to_spawn = M` where `M × 0.95 ≥ neuron_minimum_stake_e8s` and `M × 0.90 < neuron_minimum_stake_e8s`. The pre-flight check at line 2666 passes because `M × 0.95 ≥ min_stake`.
3. The child neuron enters `Spawning` state; the parent's maturity is permanently reduced.
4. After `neuron_spawn_dissolve_delay_seconds` (7 days), `maybe_spawn_neurons` fires, reads `maturity_modulation = −1000`, and calls `apply_maturity_modulation(M, −1000)` → minted stake = `M × 0.90 < neuron_minimum_stake_e8s`.
5. The resulting neuron violates the minimum-stake invariant.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** rs/nns/governance/src/governance.rs (L276-278)
```rust
const VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE: RangeInclusive<i32> =
    MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as i32
        ..=MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70 as i32;
```

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

**File:** rs/nns/governance/src/governance.rs (L6427-6447)
```rust
        let maturity_modulation = match self
            .heap_data
            .maturity_modulation
            .as_ref()
            .and_then(|m| m.current_value_permyriad)
        {
            None => return,
            Some(value) => value,
        };

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

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L43-50)
```rust
/// Maximum daily change in maturity modulation: 0.3% = 30 permyriad.
const MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD: i64 = 30;

/// Lower bound for Mission 70 maturity modulation: -10% = -1000 permyriad.
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000;

/// Upper bound for Mission 70 maturity modulation: +2% = 200 permyriad.
pub(crate) const MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70: i64 = 200;
```

**File:** rs/nervous_system/governance/src/maturity_modulation/mod.rs (L4-5)
```rust
pub const MIN_MATURITY_MODULATION_PERMYRIAD: i32 = -500;
pub const MAX_MATURITY_MODULATION_PERMYRIAD: i32 = 500;
```
