### Title
Hardcoded -5% Worst-Case Maturity Modulation in `spawn_neuron` Understates Mission 70's -10% Floor, Allowing Underfunded Spawned Neurons — (`rs/nns/governance/src/governance.rs`)

---

### Summary

The `spawn_neuron` function in NNS Governance validates that a spawned neuron will always receive at least `neuron_minimum_stake_e8s` ICP by computing a worst-case stake using a hardcoded **-5%** modulation factor. However, the Mission 70 maturity modulation system (activated in Proposal 141779) allows modulation to reach **-10%** (`MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 = -1_000`). The validation threshold and the actual execution-time modulation range are now mismatched, meaning neurons that pass the spawn guard can be spawned with a stake below the protocol's minimum.

---

### Finding Description

In `spawn_neuron`, the pre-condition check computes the minimum possible stake as:

```rust
let least_possible_stake = (maturity_to_spawn as f64 * (1_f64 - 0.05)) as u64;

if least_possible_stake < economics.neuron_minimum_stake_e8s {
    return Err(GovernanceError::new_with_message(
        ErrorType::InsufficientFunds,
        "There isn't enough maturity to spawn a new neuron due to worst case maturity modulation.",
    ));
}
``` [1](#0-0) 

The literal `0.05` encodes the old CMC-based worst case of -500 basis points (-5%). However, the Mission 70 system defines:

```rust
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000;  // -10%
pub(crate) const MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70: i64 = 200;     // +2%
``` [2](#0-1) 

The actual spawning path in `maybe_spawn_neurons` reads the Mission 70 value and validates it against the Mission 70 range:

```rust
const VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE: RangeInclusive<i32> =
    MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as i32
        ..=MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70 as i32;
``` [3](#0-2) 

So `maybe_spawn_neurons` will accept a modulation of -10% as valid and apply it:

```rust
let neuron_stake: u64 = match apply_maturity_modulation(
    original_maturity,
    maturity_modulation,
) {
``` [4](#0-3) 

The `apply_maturity_modulation` function multiplies `amount_e8s` by `(10_000 + modulation_bps) / 10_000`: [5](#0-4) 

The CHANGELOG confirms the switchover:

> Neuron spawning and maturity disbursement finalization now read the locally computed Mission 70 maturity modulation … instead of the CMC-polled `cached_daily_maturity_modulation_basis_points`. [6](#0-5) 

**Concrete gap**: A neuron with `maturity_to_spawn = M` where:

```
M * 0.90 < neuron_minimum_stake_e8s  ≤  M * 0.95
```

passes the spawn guard (because `M * 0.95 ≥ minimum`) but, when `maybe_spawn_neurons` runs with a modulation of -10%, mints only `M * 0.90` ICP — below the minimum stake invariant.

---

### Impact Explanation

**Impact: Medium**

The protocol invariant "every spawned neuron has at least `neuron_minimum_stake_e8s` ICP" is violated. The spawned neuron is permanently underfunded: it cannot be split, and its stake is below the floor the governance economics assume. No ICP is lost from the total supply (minting still occurs), but the governance state is inconsistent with its own economics parameters. This is directly analogous to the external report's pattern: a fixed-ratio guard (MCR-based reward cap / -5% modulation cap) that does not match the actual worst-case execution parameter, causing the protocol to produce an outcome outside its stated invariants.

---

### Likelihood Explanation

**Likelihood: Low**

Three conditions must coincide:
1. The Mission 70 maturity modulation must be in the range `(-1000, -500]` basis points (i.e., between -10% and -5%), which requires a sustained ICP price decline relative to the 365-day average.
2. A neuron controller must call `spawn_neuron` with a maturity amount in the narrow window `[minimum_stake / 0.95, minimum_stake / 0.90)`.
3. The spawning timer must fire while the modulation is still in that range.

The window is narrow and requires adverse market conditions, but it is reachable by any unprivileged neuron controller without special access.

---

### Recommendation

Replace the hardcoded literal `0.05` with the canonical Mission 70 minimum:

```rust
use crate::timer_tasks::MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70;

// Worst-case: apply the most negative allowed modulation.
let worst_case_modulation_bps = MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as i32;
let least_possible_stake = apply_maturity_modulation(maturity_to_spawn, worst_case_modulation_bps)
    .unwrap_or(0);

if least_possible_stake < economics.neuron_minimum_stake_e8s {
    return Err(...);
}
```

This mirrors the approach already used in SNS governance's `disburse_maturity`, which calls `apply_maturity_modulation(maturity_to_deduct, MIN_MATURITY_MODULATION_PERMYRIAD)` for its worst-case check. [7](#0-6) 

---

### Proof of Concept

**Setup**: `neuron_minimum_stake_e8s = 100_000_000` (1 ICP, the default).

**Step 1**: Controller calls `spawn_neuron` with `maturity_to_spawn = 106_000_000` (1.06 ICP).

**Step 2**: Guard computes `least_possible_stake = 106_000_000 * 0.95 = 100_700_000 ≥ 100_000_000`. Spawn is **allowed**.

**Step 3**: `maybe_spawn_neurons` fires when `maturity_modulation = -1_000` (Mission 70 minimum, -10%).

**Step 4**: `apply_maturity_modulation(106_000_000, -1_000)` = `106_000_000 * 9_000 / 10_000 = 95_400_000`.

**Result**: The spawned neuron receives **95,400,000 e8s** — below `neuron_minimum_stake_e8s` of 100,000,000 e8s. The protocol's invariant is violated. The entry path is a standard `ManageNeuron::Spawn` ingress message from any neuron controller.

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

**File:** rs/nns/governance/src/governance.rs (L6484-6488)
```rust
                    let neuron_stake: u64 = match apply_maturity_modulation(
                        original_maturity,
                        maturity_modulation,
                    ) {
                        Ok(neuron_stake) => neuron_stake,
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L47-50)
```rust
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000;

/// Upper bound for Mission 70 maturity modulation: +2% = 200 permyriad.
pub(crate) const MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70: i64 = 200;
```

**File:** rs/nervous_system/governance/src/maturity_modulation/mod.rs (L11-29)
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
}
```

**File:** rs/nns/governance/CHANGELOG.md (L14-22)
```markdown
# 2026-05-17: Proposal 141779

http://dashboard.internetcomputer.org/proposal/141779

## Changed

* Neuron spawning and maturity disbursement finalization now read the locally
  computed Mission 70 maturity modulation (derived from the XRC-backed price
  history) instead of the CMC-polled `cached_daily_maturity_modulation_basis_points`.
```

**File:** rs/sns/governance/src/governance.rs (L1654-1667)
```rust
        let worst_case_maturity_modulation =
            apply_maturity_modulation(maturity_to_deduct, MIN_MATURITY_MODULATION_PERMYRIAD)
                // Applying maturity modulation is a safe operation.
                // However, in the case that the method fails to apply the equation, return an
                // error instead of throwing a panic.
                .map_err(|err| {
                    GovernanceError::new_with_message(
                        ErrorType::PreconditionFailed,
                        format!(
                            "Could not calculate worst case maturity modulation \
                            and therefore cannot disburse maturity. Err: {err}"
                        ),
                    )
                })?;
```
