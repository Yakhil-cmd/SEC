### Title
Hardcoded -5% Worst-Case Maturity Modulation in `spawn_neuron` Is Stale After Mission 70 Expanded the Minimum to -10% — (File: rs/nns/governance/src/governance.rs)

---

### Summary

`spawn_neuron` in NNS Governance validates that a spawned neuron will meet the minimum stake requirement by assuming the worst-case maturity modulation is **-5%** (hardcoded as `1_f64 - 0.05`). Mission 70 expanded the valid modulation range so the minimum is now **-10%** (`MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 = -1_000`). The check therefore passes for maturity amounts that will produce a below-minimum stake when the actual -10% modulation is applied during `maybe_spawn_neurons`, violating the governance invariant that every neuron must hold at least `neuron_minimum_stake_e8s`.

---

### Finding Description

In `spawn_neuron`, the pre-flight guard is:

```rust
// rs/nns/governance/src/governance.rs  line 2664-2672
let least_possible_stake = (maturity_to_spawn as f64 * (1_f64 - 0.05)) as u64;

if least_possible_stake < economics.neuron_minimum_stake_e8s {
    return Err(GovernanceError::new_with_message(
        ErrorType::InsufficientFunds,
        "There isn't enough maturity to spawn a new neuron due to worst case maturity modulation.",
    ));
}
```

The literal `0.05` encodes the assumption that the worst-case modulation is **-500 permyriad (-5%)**, which matched the old constant:

```rust
// rs/nervous_system/governance/src/maturity_modulation/mod.rs  line 4-5
pub const MIN_MATURITY_MODULATION_PERMYRIAD: i32 = -500;
pub const MAX_MATURITY_MODULATION_PERMYRIAD: i32 = 500;
```

Mission 70 introduced a new, wider range for NNS governance:

```rust
// rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs  line 47-50
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000;
pub(crate) const MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70: i64 = 200;
```

`maybe_spawn_neurons` accepts any value in `VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE` (which the log message confirms is `[MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70, MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70]` = `[-1000, 200]`) and applies it unconditionally via `apply_maturity_modulation`:

```rust
// rs/nns/governance/src/governance.rs  line 6484-6502
let neuron_stake: u64 = match apply_maturity_modulation(
    original_maturity,
    maturity_modulation,
) { ... };
// ...
neuron.cached_neuron_stake_e8s = neuron_stake;
```

Because `spawn_neuron` only guards against -5% but `maybe_spawn_neurons` can apply -10%, a neuron can be created whose eventual minted stake falls below `neuron_minimum_stake_e8s`.

The same stale literal appears in `disburse_maturity`:

```
rs/nns/governance/src/governance/disburse_maturity.rs  (1 match for least_possible_stake)
```

---

### Impact Explanation

Any NNS neuron controller can call `spawn_neuron`. If their maturity falls in the window:

```
neuron_minimum_stake_e8s / 0.95  ≤  maturity_to_spawn  <  neuron_minimum_stake_e8s / 0.90
```

the pre-flight check passes (because `maturity * 0.95 ≥ minimum`), but when the actual -10% modulation is applied the resulting `neuron_stake = maturity * 0.90 < minimum`. The child neuron is created, the ledger mint is issued for the sub-minimum amount, and `cached_neuron_stake_e8s` is set to that sub-minimum value. This breaks the governance invariant that every neuron holds at least `neuron_minimum_stake_e8s`, and the minted ICP is permanently below the threshold — the neuron cannot be dissolved for the expected minimum amount.

---

### Likelihood Explanation

The maturity modulation is updated daily by the NNS governance timer task from the XRC-fed price history. The new algorithm (`compute_maturity_modulation_permyriad`) can produce values as low as -1000 permyriad (-10%) whenever the 7-day ICP price is sufficiently below the 365-day reference price. This is a realistic market condition (e.g., a sustained ICP price decline). Any neuron controller whose maturity is in the ~5.3% window between the old and new worst-case thresholds is affected. No privileged access is required; `spawn_neuron` is a standard user-callable update.

---

### Recommendation

Replace the hardcoded literal with the actual Mission 70 minimum constant so the guard is always consistent with the range enforced by `maybe_spawn_neurons`:

```rust
// Use the actual minimum modulation, not a stale hardcoded value.
let worst_case_modulation_fraction =
    1.0_f64 + (MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as f64 / 10_000.0_f64);
let least_possible_stake = (maturity_to_spawn as f64 * worst_case_modulation_fraction) as u64;
```

Apply the same fix to the analogous check in `disburse_maturity`. Alternatively, derive the constant from `VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE.start()` so future range changes automatically propagate.

---

### Proof of Concept

Assume `neuron_minimum_stake_e8s = 100_000_000` (1 ICP, the NNS default).

1. User holds a neuron with `maturity_e8s_equivalent = 106_000_000` (~1.06 ICP).
2. User calls `spawn_neuron` with `percentage_to_spawn = 100`.
3. `maturity_to_spawn = 106_000_000`.
4. Guard: `106_000_000 * 0.95 = 100_700_000 ≥ 100_000_000` → **passes**.
5. Child neuron is created in spawning state with `maturity_e8s_equivalent = 106_000_000`.
6. Next day, `maturity_modulation = -1_000` permyriad (-10%) — within the Mission 70 valid range.
7. `maybe_spawn_neurons` calls `apply_maturity_modulation(106_000_000, -1_000)`.
8. Result: `106_000_000 * (10_000 - 1_000) / 10_000 = 106_000_000 * 0.90 = 95_400_000`.
9. `neuron.cached_neuron_stake_e8s = 95_400_000 < 100_000_000 = neuron_minimum_stake_e8s`.
10. The ledger mints 95_400_000 e8s to the child neuron — **below the minimum stake invariant**. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/nns/governance/src/governance.rs (L2664-2672)
```rust
        // Check if the least possible stake this neuron would be spawned with
        // is more than the minimum neuron stake.
        let least_possible_stake = (maturity_to_spawn as f64 * (1_f64 - 0.05)) as u64;

        if least_possible_stake < economics.neuron_minimum_stake_e8s {
            return Err(GovernanceError::new_with_message(
                ErrorType::InsufficientFunds,
                "There isn't enough maturity to spawn a new neuron due to worst case maturity modulation.",
            ));
```

**File:** rs/nns/governance/src/governance.rs (L6438-6447)
```rust
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

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L47-50)
```rust
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000;

/// Upper bound for Mission 70 maturity modulation: +2% = 200 permyriad.
pub(crate) const MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70: i64 = 200;
```

**File:** rs/nervous_system/governance/src/maturity_modulation/mod.rs (L4-5)
```rust
pub const MIN_MATURITY_MODULATION_PERMYRIAD: i32 = -500;
pub const MAX_MATURITY_MODULATION_PERMYRIAD: i32 = 500;
```
