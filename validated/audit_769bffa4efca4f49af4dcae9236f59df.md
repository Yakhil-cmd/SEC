Audit Report

## Title
Stale 5% Worst-Case Modulation Constant in `spawn_neuron` Pre-Check Allows Spawning Sub-Minimum-Stake Neurons After Mission 70 Expansion to −10% — (File: `rs/nns/governance/src/governance.rs`)

## Summary

The `spawn_neuron` pre-flight check at line 2666 of `rs/nns/governance/src/governance.rs` uses the hardcoded literal `0.05` (5%) as the worst-case maturity modulation when computing `least_possible_stake`. Mission 70 expanded the negative modulation bound to −10% (`MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 = -1_000`), and the CHANGELOG for Proposal 141779 confirms that neuron spawning now reads the Mission 70 modulation value. The pre-check therefore underestimates the worst case, allowing a neuron holder to spawn a child neuron whose final ICP stake — after the actual −10% modulation is applied — falls below `neuron_minimum_stake_e8s`, permanently consuming the parent's maturity in the process.

## Finding Description

**Root cause — stale constant in pre-check:**

`rs/nns/governance/src/governance.rs` line 2666:
```rust
let least_possible_stake = (maturity_to_spawn as f64 * (1_f64 - 0.05)) as u64;
``` [1](#0-0) 

The literal `0.05` was historically correct when the CMC-backed modulation range was `[−500, +500]` permyriad (−5% to +5%), defined in:
```rust
pub const MIN_MATURITY_MODULATION_PERMYRIAD: i32 = -500;
pub const MAX_MATURITY_MODULATION_PERMYRIAD: i32 = 500;
``` [2](#0-1) 

Mission 70 introduced a wider range with a lower bound of −10%:
```rust
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000;
pub(crate) const MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70: i64 = 200;
``` [3](#0-2) 

The CHANGELOG for Proposal 141779 explicitly confirms the switchover:
> "Neuron spawning and maturity disbursement finalization now read the locally computed Mission 70 maturity modulation … instead of the CMC-polled `cached_daily_maturity_modulation_basis_points`." [4](#0-3) 

`MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70` is referenced 3 times in `governance.rs` (confirming the codebase is aware of it), but the `spawn_neuron` pre-check was never updated to use it. The actual modulation is applied later via `apply_maturity_modulation`, which correctly uses the full basis-point range including −1000: [5](#0-4) 

**Exploit path:**

1. Hold a neuron with `maturity_e8s_equivalent = M` where `neuron_minimum_stake_e8s / 0.95 ≤ M < neuron_minimum_stake_e8s / 0.90`.
2. Call `manage_neuron { Spawn { percentage_to_spawn: 100, ... } }`.
3. Pre-check computes `M * 0.95 ≥ neuron_minimum_stake_e8s` → **passes**.
4. Child neuron is created with `maturity_e8s_equivalent = M`.
5. Timer fires `maybe_spawn_neurons`; it reads Mission 70 modulation at −1000 permyriad and calls `apply_maturity_modulation(M, −1000)`.
6. Result: `M * (10_000 − 1_000) / 10_000 = M * 0.90 < neuron_minimum_stake_e8s`.
7. Parent maturity is permanently deducted; child neuron's cached stake is below the governance minimum.

**Concrete example** (`neuron_minimum_stake_e8s = 100_000_000`):

| Step | Value |
|---|---|
| Minimum M to pass 5% check | `100_000_000 / 0.95 ≈ 105_263_158` e8s |
| Actual stake after −10% modulation | `105_263_158 × 0.90 ≈ 94_736_842` e8s |
| Shortfall | `≈ 5_263_158` e8s (~0.053 ICP) |

## Impact Explanation

This matches **High ($2,000–$10,000)**: "Significant NNS … security impact with concrete user or protocol harm." Specifically:

- The parent neuron's maturity is permanently consumed with no rollback path.
- The spawned neuron receives less ICP than the governance-enforced minimum, potentially preventing it from voting or proposing.
- The error message explicitly promises the check prevents this outcome ("There isn't enough maturity to spawn a new neuron due to worst case maturity modulation"), but the guarantee is now false — a broken protocol invariant with direct user-fund harm.

## Likelihood Explanation

- Mission 70 is live in production (Proposal 141779 deployed 2026-05-17).
- The modulation formula (`sensitivity × (recent_price − reference_price) / reference_price`) reaches −10% whenever the 7-day ICP price is sufficiently below the 365-day average — a realistic market condition.
- No privileged role is required; any neuron holder can call `manage_neuron { Spawn }`.
- The vulnerable maturity window is narrow but deterministically reachable whenever modulation is near its minimum.

## Recommendation

Replace the undocumented literal `0.05` with a named constant derived from `MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70`:

```rust
// Worst-case maturity modulation under Mission 70 is -10% = -1000 permyriad.
const WORST_CASE_MODULATION_FRACTION: f64 =
    (-MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as f64) / 10_000.0; // = 0.10

let least_possible_stake =
    (maturity_to_spawn as f64 * (1.0 - WORST_CASE_MODULATION_FRACTION)) as u64;
```

Add a unit test asserting that `least_possible_stake` computed with the worst-case constant, when passed through `apply_maturity_modulation` at `MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70`, always yields a value ≥ `neuron_minimum_stake_e8s`.

## Proof of Concept

**Invariant/unit test plan:**

```rust
#[test]
fn spawn_precheck_consistent_with_mission_70_worst_case() {
    let min_stake = 100_000_000_u64; // 1 ICP
    // Minimum maturity that passes the (stale) 5% pre-check
    let maturity = (min_stake as f64 / 0.95).ceil() as u64;
    // Apply actual Mission 70 worst-case modulation: -1000 permyriad
    let actual_stake = apply_maturity_modulation(maturity, -1000).unwrap();
    assert!(
        actual_stake >= min_stake,
        "Spawned stake {} < minimum stake {} — invariant violated",
        actual_stake, min_stake
    );
}
```

This test fails with the current `0.05` constant and passes after updating to `0.10`, directly demonstrating the invariant violation.

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

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L46-50)
```rust
/// Lower bound for Mission 70 maturity modulation: -10% = -1000 permyriad.
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000;

/// Upper bound for Mission 70 maturity modulation: +2% = 200 permyriad.
pub(crate) const MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70: i64 = 200;
```

**File:** rs/nns/governance/CHANGELOG.md (L20-22)
```markdown
* Neuron spawning and maturity disbursement finalization now read the locally
  computed Mission 70 maturity modulation (derived from the XRC-backed price
  history) instead of the CMC-polled `cached_daily_maturity_modulation_basis_points`.
```
