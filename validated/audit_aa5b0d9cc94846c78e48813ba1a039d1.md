All cited code is verified. Here is the validation result:

---

Audit Report

## Title
Stale 5% Worst-Case Maturity Modulation Constant in `spawn_neuron` Allows Below-Minimum-Stake Neuron Creation - (File: `rs/nns/governance/src/governance.rs`)

## Summary

`spawn_neuron` gates neuron creation with a hardcoded 5% worst-case modulation floor, but `maybe_spawn_neurons` now applies Mission 70 modulation whose lower bound is −10% (−1,000 permyriad). A neuron controller can pass the pre-check with maturity that will produce a spawned neuron with `cached_neuron_stake_e8s` below `neuron_minimum_stake_e8s`, permanently violating the protocol invariant that every neuron holds at least the minimum stake.

## Finding Description

In `rs/nns/governance/src/governance.rs` at line 2666, `spawn_neuron` computes the minimum possible spawned stake as:

```rust
let least_possible_stake = (maturity_to_spawn as f64 * (1_f64 - 0.05)) as u64;
```

This hardcodes 5% as the worst-case modulation. However, `maybe_spawn_neurons` (lines 6427–6502) reads `heap_data.maturity_modulation.current_value_permyriad` — the Mission 70 locally-computed value — and passes it directly to `apply_maturity_modulation`. The Mission 70 lower bound is `MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 = -1_000` permyriad (−10%), defined in `rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs` at line 47. The sanity check in `maybe_spawn_neurons` (line 6438) uses `VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE` whose lower bound is this same −1,000 permyriad value, confirming that −10% is a valid, accepted execution-time modulation.

`apply_maturity_modulation` in `rs/nervous_system/governance/src/maturity_modulation/mod.rs` computes `amount * (10_000 + basis_points) / 10_000`, so at −1,000 permyriad the spawned stake is 90% of maturity. The pre-check permits maturity where `maturity * 0.95 >= neuron_minimum_stake_e8s`, but execution can produce `maturity * 0.90 < neuron_minimum_stake_e8s`. The CHANGELOG entry at line 20 of `rs/nns/governance/CHANGELOG.md` confirms the switch to Mission 70 modulation was intentional; the pre-check was never updated.

The SNS `disburse_maturity` (lines 1654–1667 of `rs/sns/governance/src/governance.rs`) demonstrates the correct pattern: it calls `apply_maturity_modulation` with the actual worst-case constant for its pre-check. NNS `spawn_neuron` does not follow this pattern and was not updated when Mission 70 was introduced.

## Impact Explanation

An unprivileged neuron controller can permanently create a neuron whose `cached_neuron_stake_e8s` is below `neuron_minimum_stake_e8s`. With `neuron_minimum_stake_e8s = 100_000_000 e8s` (1 ICP), any maturity in `[105_263_158, 111_111_111)` e8s passes the pre-check but produces a sub-minimum-stake neuron when modulation is between −500 and −1,000 permyriad. The neuron is minted in this state with no post-spawn correction path. This constitutes a moderate user-funds and protocol-invariant impact: the spawned neuron permanently holds less ICP than the protocol minimum, which may affect downstream governance operations that assume the invariant holds. This matches the Medium allowed impact: "moderate user-funds/security impact."

## Likelihood Explanation

The Mission 70 modulation moves at most 30 permyriad/day. Reaching −500 permyriad (where the gap opens) takes approximately 17 days of sustained ICP price decline from zero. Once below −500 permyriad, any neuron controller with maturity in the ~5.5% window above `neuron_minimum_stake_e8s / 0.95` can trigger the bug via a standard `manage_neuron` ingress call — no privileged access required. The condition is realistic for active NNS participants and repeatable for as long as modulation remains below −500 permyriad.

## Recommendation

Replace the hardcoded `0.05` with the actual Mission 70 worst-case modulation constant, mirroring the SNS pattern:

```rust
use crate::timer_tasks::update_icp_xdr_rate_related_data::MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70;

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

## Proof of Concept

1. Wait for Mission 70 maturity modulation to reach −600 permyriad (achievable after ~20 days of sustained ICP price decline).
2. As a neuron controller with `maturity_e8s_equivalent = 106_000_000` e8s:
   - Pre-check: `106_000_000 * 0.95 = 100_700_000 >= 100_000_000` → **passes** (line 2668).
3. Call `manage_neuron` → `Spawn { percentage_to_spawn: 100, ... }`. Child neuron enters spawning state with `maturity_e8s_equivalent = 106_000_000`.
4. When `maybe_spawn_neurons` fires with modulation = −600 permyriad:
   - `apply_maturity_modulation(106_000_000, -600)` = `106_000_000 * 9_400 / 10_000 = 99_640_000`.
5. Child neuron is minted with `cached_neuron_stake_e8s = 99_640_000 < 100_000_000 = neuron_minimum_stake_e8s`.

A deterministic integration test can reproduce this by setting `heap_data.maturity_modulation.current_value_permyriad = Some(-600)` and calling `maybe_spawn_neurons` on a neuron in spawning state with the above maturity value, then asserting `cached_neuron_stake_e8s < neuron_minimum_stake_e8s`.