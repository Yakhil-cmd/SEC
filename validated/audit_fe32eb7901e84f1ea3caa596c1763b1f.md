### Title
Spawn Neuron Minimum Stake Guard Uses Hardcoded 5% Worst-Case While Actual Modulation Floor Is -10% — (`rs/nns/governance/src/governance.rs`)

---

### Summary

`spawn_neuron` guards against sub-minimum spawned stakes by computing `least_possible_stake` with a hardcoded 5% discount. The Mission 70 maturity modulation system, however, defines a minimum of **-10%** (`MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 = -1_000`). Any neuron controller whose `maturity_to_spawn` falls in the band `[neuron_minimum_stake_e8s / 0.95, neuron_minimum_stake_e8s / 0.90)` will pass the pre-spawn guard but receive a minted stake below `neuron_minimum_stake_e8s` when `maybe_spawn_neurons` applies the actual -10% modulation.

---

### Finding Description

**Guard in `spawn_neuron` (governance.rs:2666):**

```rust
let least_possible_stake = (maturity_to_spawn as f64 * (1_f64 - 0.05)) as u64;
if least_possible_stake < economics.neuron_minimum_stake_e8s {
    return Err(...);
}
``` [1](#0-0) 

The constant `0.05` encodes a -5% worst-case assumption.

**Actual minimum modulation (update_icp_xdr_rate_related_data.rs:47):**

```rust
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000; // -10%
``` [2](#0-1) 

**Valid range used by `maybe_spawn_neurons` (governance.rs:276-278):**

```rust
const VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE: RangeInclusive<i32> =
    MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as i32
        ..=MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70 as i32;
``` [3](#0-2) 

`maybe_spawn_neurons` accepts any modulation in `[-1000, 200]` as valid and applies it unconditionally via `apply_maturity_modulation`: [4](#0-3) [5](#0-4) 

`apply_maturity_modulation` itself performs no minimum-stake check: [6](#0-5) 

---

### Impact Explanation

With `neuron_minimum_stake_e8s = 100_000_000` (1 ICP, the default):

| Step | Value |
|---|---|
| Minimum `maturity_to_spawn` to pass 5% guard | `⌈100_000_000 / 0.95⌉ = 105_263_158` |
| Stake after -10% modulation at that value | `105_263_158 × 0.90 = 94_736_842` |
| Shortfall vs. minimum stake | **5_263_158 e8s (~0.053 ICP)** |

The spawned neuron is created on-chain with `cached_neuron_stake_e8s < neuron_minimum_stake_e8s`, violating the protocol invariant. The neuron exists in a degraded state: it cannot be split (split checks minimum stake), and it permanently holds sub-minimum ICP that the controller cannot recover through normal governance flows without dissolving.

---

### Likelihood Explanation

- Reachable by **any** neuron controller with sufficient maturity — no privileged role required.
- The trigger condition (modulation between -5% and -10%) is within the documented and enforced valid range of the Mission 70 algorithm.
- The speed limit of 30 permyriad/day means reaching -1000 takes ~33 days of sustained ICP price decline, which is a realistic market scenario.
- The attacker only needs to call `manage_neuron { Spawn { ... } }` via ingress when modulation is in the vulnerable band.

---

### Recommendation

Replace the hardcoded `0.05` with the actual minimum modulation constant:

```rust
// Before
let least_possible_stake = (maturity_to_spawn as f64 * (1_f64 - 0.05)) as u64;

// After — use the actual Mission 70 floor
let worst_case_modulation_bps = MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70; // -1000
let least_possible_stake = apply_maturity_modulation(
    maturity_to_spawn,
    worst_case_modulation_bps as i32,
).unwrap_or(0);
```

This mirrors the correct pattern already used in SNS governance's `disburse_maturity`, which calls `apply_maturity_modulation(maturity_to_deduct, MIN_MATURITY_MODULATION_PERMYRIAD)` for its worst-case check. [7](#0-6) 

---

### Proof of Concept

```
neuron_minimum_stake_e8s = 100_000_000

// Step 1: controller calls spawn_neuron with maturity just above 5% threshold
maturity_to_spawn = 105_263_159

// spawn_neuron check passes:
least_possible_stake = floor(105_263_159 * 0.95) = 100_000_001 >= 100_000_000 ✓

// Step 2: ~33 days later, modulation reaches -1000 (-10%)
// maybe_spawn_neurons accepts -1000 as within VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE

// Step 3: apply_maturity_modulation(105_263_159, -1000)
neuron_stake = floor(105_263_159 * (10_000 - 1_000) / 10_000)
             = floor(105_263_159 * 9_000 / 10_000)
             = 94_736_843

// Invariant violated:
94_736_843 < 100_000_000  ← spawned neuron stake below neuron_minimum_stake_e8s
```

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

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L46-50)
```rust
/// Lower bound for Mission 70 maturity modulation: -10% = -1000 permyriad.
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
