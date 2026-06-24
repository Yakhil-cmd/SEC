Audit Report

## Title
Hardcoded 5% Worst-Case Guard in `spawn_neuron` Allows Sub-Minimum-Stake Neurons Under Mission 70's -10% Modulation — (`rs/nns/governance/src/governance.rs`)

## Summary

`spawn_neuron` gates the spawn on a hardcoded 5% worst-case modulation check, but Mission 70 extended the actual enforced minimum modulation to -10% (-1000 permyriad). Any neuron owner whose maturity satisfies the 5% guard but not the 10% guard can call `spawn_neuron`, wait 7 days, and have `maybe_spawn_neurons` mint a child neuron whose `cached_neuron_stake_e8s` is below `neuron_minimum_stake_e8s`, violating the minimum-stake invariant enforced everywhere else in NNS governance.

## Finding Description

**Root cause — stale constant in `spawn_neuron`:**

The guard at line 2666 of `rs/nns/governance/src/governance.rs` computes the worst-case stake using a hardcoded `0.05` (5%):

```rust
let least_possible_stake = (maturity_to_spawn as f64 * (1_f64 - 0.05)) as u64;
if least_possible_stake < economics.neuron_minimum_stake_e8s { ... }
``` [1](#0-0) 

**Mission 70 extended the actual minimum to -10%:**

`MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 = -1_000` is defined and is the actual lower bound applied by `compute_maturity_modulation_permyriad`: [2](#0-1) 

`compute_maturity_modulation_permyriad` clamps its output to this range as the final step, making -1000 permyriad a reachable, protocol-enforced steady state: [3](#0-2) 

**`apply_maturity_modulation` faithfully applies -10%:**

The function multiplies `amount * (10_000 + basis_points) / 10_000`. At `basis_points = -1000` this yields `amount * 9_000 / 10_000 = amount * 0.90`. The stored modulation field `cached_daily_maturity_modulation_basis_points: Option<i32>` can hold -1000, and `apply_maturity_modulation` accepts `i32`: [4](#0-3) 

**The gap:** `spawn_neuron` checks `maturity * 0.95 >= min_stake`, but `maybe_spawn_neurons` can apply up to -10%, minting only `maturity * 0.90`. Any maturity `M` satisfying `M * 0.95 >= min_stake AND M * 0.90 < min_stake` passes the gate but produces a sub-minimum-stake child neuron. The old constants in `rs/nervous_system/governance/src/maturity_modulation/mod.rs` (`MIN_MATURITY_MODULATION_PERMYRIAD = -500`) are no longer the operative bounds for NNS governance: [5](#0-4) 

## Impact Explanation

This is a **High** severity NNS governance integrity issue. Sub-minimum-stake neurons are created in violation of the invariant that governance enforces everywhere else. The child neuron holds less ICP than `neuron_minimum_stake_e8s`, meaning it cannot be dissolved for the expected minimum ICP. At scale (thousands of neurons in the maturity band during a sustained low-price period), the aggregate minted ICP across all such spawns is less than the maturity burned, breaking ledger conservation expectations. This constitutes a significant NNS governance security impact with concrete user and protocol harm, matching the "High — Significant NNS governance security impact with concrete user or protocol harm" bounty class.

## Likelihood Explanation

- The attacker is an unprivileged neuron owner — no special role required.
- The entrypoint is the standard `manage_neuron` ingress call.
- The condition requires modulation to be at or near -1000 permyriad, which is a reachable protocol state when the 7-day ICP price is significantly below the 365-day average for an extended period.
- The maturity band is narrow but non-empty; with thousands of neurons, some will fall in it during adverse market conditions.
- The 7-day delay between spawn and mint means the modulation at mint time may differ from spawn time, but the worst case is fully reachable.

## Recommendation

Replace the hardcoded `0.05` in `spawn_neuron` with the actual Mission 70 minimum modulation constant, eliminating the floating-point conversion:

```rust
use crate::timer_tasks::update_icp_xdr_rate_related_data::MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70;
let worst_case_factor = (10_000_i64 + MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70) as u64;
let least_possible_stake = maturity_to_spawn
    .saturating_mul(worst_case_factor)
    / 10_000;
```

This keeps the guard in sync with the actual modulation bounds enforced by `compute_maturity_modulation_permyriad`.

## Proof of Concept

Let `min_stake = 100_000_000` (1 ICP in e8s). Choose `maturity = 106_000_000` (1.06 ICP).

1. **Spawn check (passes):** `106_000_000 * 0.95 = 100_700_000 >= 100_000_000` → spawn is accepted.
2. **Actual mint at -1000 permyriad:** `apply_maturity_modulation(106_000_000, -1000)` = `106_000_000 * 9_000 / 10_000 = 95_400_000`.
3. **Result:** `95_400_000 < 100_000_000` → child neuron is below minimum stake.

A state-machine test setting `cached_daily_maturity_modulation_basis_points = Some(-1000)` and spawning a neuron with this maturity will confirm `child_neuron.cached_neuron_stake_e8s < neuron_minimum_stake_e8s` after `maybe_spawn_neurons` executes.

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

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L46-47)
```rust
/// Lower bound for Mission 70 maturity modulation: -10% = -1000 permyriad.
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000;
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L191-196)
```rust
    // Global bounds have final say. The result is within [MIN, MAX] which fit in i64, so the
    // cast is safe.
    Ok(speed_limited.clamp(
        MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as i128,
        MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70 as i128,
    ) as i64)
```

**File:** rs/nervous_system/governance/src/maturity_modulation/mod.rs (L4-5)
```rust
pub const MIN_MATURITY_MODULATION_PERMYRIAD: i32 = -500;
pub const MAX_MATURITY_MODULATION_PERMYRIAD: i32 = 500;
```

**File:** rs/nervous_system/governance/src/maturity_modulation/mod.rs (L11-28)
```rust
pub fn apply_maturity_modulation(
    amount_maturity_e8s: u64,
    maturity_modulation_basis_points: i32,
) -> Result<u64, String> {
    let amount_e8s = u128::from(amount_maturity_e8s);

    let adjusted_maturity_modulation_basis_points = saturating_add_or_subtract_u128_i32(
        BASIS_POINTS_PER_UNITY,
        maturity_modulation_basis_points,
    );

    let modulated_amount_e8s: u128 = amount_e8s
        .checked_mul(adjusted_maturity_modulation_basis_points)
        .ok_or_else(|| "Underflow or overflow when calculating maturity modulation".to_string())?
        .checked_div(BASIS_POINTS_PER_UNITY)
        .ok_or_else(|| "Underflow or overflow when calculating maturity modulation".to_string())?;

    u64::try_from(modulated_amount_e8s).map_err(|err| err.to_string())
```
