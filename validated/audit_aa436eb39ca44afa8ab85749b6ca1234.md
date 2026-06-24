Audit Report

## Title
`spawn_neuron` Pre-flight Guard Uses Stale 5% Worst-Case Modulation While Mission 70 Settlement Applies Up to −10% - (File: `rs/nns/governance/src/governance.rs`)

## Summary
`Governance::spawn_neuron` validates the spawned neuron's minimum stake using a hardcoded 5% worst-case modulation constant, but `maybe_spawn_neurons` settles 7 days later using the Mission 70 maturity modulation system whose lower bound is −10%. Any neuron holder with maturity in the window `[neuron_minimum_stake_e8s / 0.95, neuron_minimum_stake_e8s / 0.90)` can pass the pre-flight check and have their maturity irrevocably committed to a child neuron that is later minted with a stake below `neuron_minimum_stake_e8s`, violating the NNS neuron-stake invariant.

## Finding Description

**Pre-flight check (`spawn_neuron`, line 2666):**

```rust
let least_possible_stake = (maturity_to_spawn as f64 * (1_f64 - 0.05)) as u64;
if least_possible_stake < economics.neuron_minimum_stake_e8s {
    return Err(...);
}
```

The constant `0.05` (5%) was correct when the only modulation source was the CMC, whose range is `[MIN_MATURITY_MODULATION_PERMYRIAD, MAX_MATURITY_MODULATION_PERMYRIAD]` = `[−500, +500]` permyriad = ±5% (`rs/nervous_system/governance/src/maturity_modulation/mod.rs`, lines 4–5).

**Settlement (`maybe_spawn_neurons`, lines 6427–6447):**

The function reads `heap_data.maturity_modulation.current_value_permyriad` and validates it against `VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE`, which is defined as:

```rust
const VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE: RangeInclusive<i32> =
    MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as i32
        ..=MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70 as i32;
```

where `MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 = -1_000` (−10%) (`rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs`, line 47). The actual minting call at line 6484 passes this value directly to `apply_maturity_modulation`, which computes `amount * (10_000 + modulation) / 10_000`. At −1000 permyriad this yields `amount * 0.90`.

**The gap:** the pre-flight guard rejects only if `maturity * 0.95 < min_stake`, but settlement can apply `maturity * 0.90`. Any maturity `M` satisfying `M * 0.95 ≥ min_stake` and `M * 0.90 < min_stake` passes the guard and later produces a sub-minimum neuron. For `neuron_minimum_stake_e8s = 100_000_000` (1 ICP), the exploitable window is approximately `[105_263_158, 111_111_111]` e8s.

Once the child neuron enters `Spawning` state, the parent's maturity is permanently reduced (line 6515: `neuron.maturity_e8s_equivalent = 0`). There is no rollback path if the minted stake falls below the minimum.

## Impact Explanation

This is a significant NNS protocol impact with concrete user harm. An unprivileged neuron controller can permanently commit maturity to a child neuron that is subsequently minted with a stake below `neuron_minimum_stake_e8s`, violating the NNS neuron-stake invariant. The user's maturity is irrevocably consumed and the resulting neuron holds less ICP than the protocol minimum. This constitutes concrete user harm and a protocol correctness violation in NNS governance, fitting the **High ($2,000–$10,000)** impact class: "Significant NNS security impact with concrete user or protocol harm."

## Likelihood Explanation

Mission 70 modulation starts at 0 and is speed-limited to 30 permyriad per day (`MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD = 30`). Reaching −10% requires approximately 33 consecutive days of ICP price decline relative to the 365-day average — a realistic bear-market scenario. Once modulation is at or near −1000 permyriad, the exploit window is open to any neuron holder with maturity in the vulnerable range. No special privileges are required; any neuron controller can trigger it.

## Recommendation

Replace the hardcoded `0.05` constant in `spawn_neuron` with the actual Mission 70 lower bound so the pre-flight check is consistent with what `maybe_spawn_neurons` will apply at settlement:

```rust
use ic_nervous_system_governance::maturity_modulation::apply_maturity_modulation;
let least_possible_stake = apply_maturity_modulation(
    maturity_to_spawn,
    MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as i32,
).unwrap_or(0);
if least_possible_stake < economics.neuron_minimum_stake_e8s {
    return Err(GovernanceError::new_with_message(
        ErrorType::InsufficientFunds,
        "There isn't enough maturity to spawn a new neuron due to worst case maturity modulation.",
    ));
}
```

This ensures the guard always uses the same worst-case bound as the settlement path.

## Proof of Concept

1. Set `heap_data.maturity_modulation.current_value_permyriad = Some(-1000)` in a test environment (or wait 33+ days of price decline on mainnet).
2. Call `spawn_neuron` with `maturity_to_spawn = M` where `M * 0.95 ≥ neuron_minimum_stake_e8s` and `M * 0.90 < neuron_minimum_stake_e8s` (e.g., `M = 106_000_000` with `min_stake = 100_000_000`).
3. The pre-flight check at line 2666 computes `106_000_000 * 0.95 = 100_700_000 ≥ 100_000_000` — passes.
4. The child neuron enters `Spawning` state; the parent's maturity is permanently reduced.
5. After `neuron_spawn_dissolve_delay_seconds` (7 days), `maybe_spawn_neurons` fires, reads `maturity_modulation = −1000`, and calls `apply_maturity_modulation(106_000_000, −1000)` → minted stake = `95_400_000 < 100_000_000 = neuron_minimum_stake_e8s`.
6. The resulting neuron violates the minimum-stake invariant with no recovery path.