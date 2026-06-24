All cited code locations are confirmed. Let me verify one more detail about `apply_maturity_modulation` behavior with -1000 permyriad.

Audit Report

## Title
`spawn_neuron` Pre-flight Guard Uses Hardcoded 5% Worst-Case Modulation While Mission 70 Settlement Allows −10% - (File: `rs/nns/governance/src/governance.rs`)

## Summary
`Governance::spawn_neuron` validates the spawned neuron's minimum stake using a hardcoded `0.05` (5%) worst-case modulation constant, but `maybe_spawn_neurons` settles 7 days later using the Mission 70 modulation system whose lower bound is −10% (`MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 = -1_000`). Any neuron holder with maturity in the window `[min_stake / 0.95, min_stake / 0.90)` can initiate a spawn that passes the pre-flight check but settles with a stake below `neuron_minimum_stake_e8s`, permanently violating the NNS neuron minimum-stake invariant.

## Finding Description

**Pre-flight check (`spawn_neuron`, line 2666):**

```rust
let least_possible_stake = (maturity_to_spawn as f64 * (1_f64 - 0.05)) as u64;
if least_possible_stake < economics.neuron_minimum_stake_e8s { ... }
```

The constant `0.05` reflects the old CMC-era range `[MIN_MATURITY_MODULATION_PERMYRIAD, MAX_MATURITY_MODULATION_PERMYRIAD]` = `[−500, +500]` permyriad (±5%), defined in `rs/nervous_system/governance/src/maturity_modulation/mod.rs` lines 4–5. It was never updated for Mission 70.

**Settlement (`maybe_spawn_neurons`, lines 6427–6447 and 6484–6487):**

`maybe_spawn_neurons` reads `heap_data.maturity_modulation.current_value_permyriad` and validates it against `VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE`, which is defined at lines 276–278 as:

```rust
const VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE: RangeInclusive<i32> =
    MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as i32
        ..=MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70 as i32;
```

`MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 = -1_000` (−10%), defined at line 47 of `rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs`. The settlement then calls:

```rust
let neuron_stake: u64 = match apply_maturity_modulation(
    original_maturity,
    maturity_modulation,   // can be −1000 permyriad
) { ... };
```

`apply_maturity_modulation` with `maturity_modulation_basis_points = -1000` computes `amount × (10000 − 1000) / 10000 = amount × 0.90`.

**The gap:** For any maturity value M satisfying `M × 0.95 ≥ neuron_minimum_stake_e8s` and `M × 0.90 < neuron_minimum_stake_e8s`, the pre-flight check at line 2668 passes, the parent neuron's maturity is irrevocably reduced, the child enters `Spawning` state, and 7 days later settlement mints a stake below `neuron_minimum_stake_e8s`. No existing guard in `maybe_spawn_neurons` checks the resulting stake against the minimum before minting.

## Impact Explanation

This is a **High** severity NNS security impact. Any unprivileged neuron controller can trigger it without special access. The concrete harms are: (1) the user's maturity is permanently and irrevocably transferred from the parent neuron to a child neuron in spawning state — this transfer cannot be reversed; (2) the child neuron is minted with a stake below `neuron_minimum_stake_e8s`, violating the NNS protocol invariant that every neuron must hold at least the minimum stake. A neuron below minimum stake may be inoperable for standard governance operations (staking top-ups, merging, etc.) and represents a permanent loss of the maturity delta. This matches the allowed impact: "Significant NNS security impact with concrete user or protocol harm."

## Likelihood Explanation

Mission 70 modulation starts at 0 and is speed-limited to 30 permyriad/day (`MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD = 30`). Reaching −10% requires approximately 33 consecutive days of ICP price decline relative to the 365-day average — a realistic bear-market scenario. Once modulation is at or near −1000 permyriad, the vulnerable window is open to any neuron holder whose maturity falls in `[min_stake / 0.95, min_stake / 0.90)`. No privileged access is required; the attacker only needs to be a neuron controller with maturity in that range.

## Recommendation

Replace the hardcoded `0.05` constant in `spawn_neuron` (line 2666 of `rs/nns/governance/src/governance.rs`) with the actual Mission 70 lower bound:

```rust
use ic_nervous_system_governance::maturity_modulation::apply_maturity_modulation;
let least_possible_stake = apply_maturity_modulation(
    maturity_to_spawn,
    MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as i32,
).unwrap_or(0);
```

This ensures the pre-flight check is always consistent with the worst-case value that `maybe_spawn_neurons` can legally apply at settlement time.

## Proof of Concept

1. Set `heap_data.maturity_modulation.current_value_permyriad = Some(-1000)` in a test (or wait 33+ days of price decline on mainnet).
2. Let `min_stake = economics.neuron_minimum_stake_e8s`. Choose `M` such that `M × 0.95 ≥ min_stake` and `M × 0.90 < min_stake` (e.g., `M = min_stake × 10 / 9 − 1`).
3. Call `spawn_neuron` with `maturity_to_spawn = M`. The check at line 2668 passes because `M × 0.95 ≥ min_stake`.
4. The parent neuron's maturity is permanently reduced by `M`; the child neuron enters `Spawning` state.
5. After `neuron_spawn_dissolve_delay_seconds` (7 days), `maybe_spawn_neurons` fires, reads `maturity_modulation = −1000`, calls `apply_maturity_modulation(M, −1000)` → minted stake = `M × 0.90 < min_stake`.
6. Assert the child neuron's `cached_neuron_stake_e8s < neuron_minimum_stake_e8s` — invariant violated.