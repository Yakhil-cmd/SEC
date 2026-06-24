### Title
Spawn Neuron Pre-Check Uses Stale -5% Worst-Case Modulation After Mission 70 Extended Minimum to -10% - (File: `rs/nns/governance/src/governance.rs`)

### Summary

The `spawn_neuron` pre-condition check in NNS Governance hardcodes -5% as the worst-case maturity modulation when validating whether a neuron has sufficient maturity to spawn. Mission 70 extended the minimum maturity modulation to -10% (`MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 = -1_000`), but the spawn check was never updated. This inconsistency allows a neuron owner to spawn a child neuron whose actual minted stake falls below `neuron_minimum_stake_e8s`, violating a core protocol invariant.

### Finding Description

**Root cause — stale constant in the spawn pre-check:**

In `rs/nns/governance/src/governance.rs`, `spawn_neuron` validates that the maturity being spawned is large enough to survive the worst-case modulation:

```rust
// rs/nns/governance/src/governance.rs ~line 2666
let least_possible_stake = (maturity_to_spawn as f64 * (1_f64 - 0.05)) as u64;

if least_possible_stake < economics.neuron_minimum_stake_e8s {
    return Err(GovernanceError::new_with_message(
        ErrorType::InsufficientFunds,
        "There isn't enough maturity to spawn a new neuron due to worst case maturity modulation.",
    ));
}
```

The factor `(1_f64 - 0.05)` encodes a worst-case modulation of **-5% (-500 permyriad)**. This was correct when the CMC-backed modulation was bounded to `[-500, +500]` permyriad (the old `MIN_MATURITY_MODULATION_PERMYRIAD = -500` in `rs/nervous_system/governance/src/maturity_modulation/mod.rs`).

**Mission 70 changed the bounds without updating the check:**

The new NNS Governance-local computation (`compute_maturity_modulation_permyriad` in `rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs`) uses:

```rust
// rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000; // -10%
pub(crate) const MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70: i64 = 200;    // +2%
```

The CHANGELOG confirms that as of Proposal 141779, spawning now reads `maturity_modulation.current_value_permyriad` (the Mission 70 value, bounded to `[-1000, +200]`), not the old CMC-polled value. The `maybe_spawn_neurons` function enforces this range:

```rust
// rs/nns/governance/src/governance.rs ~line 6438
if !VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE.contains(&maturity_modulation) {
    // logs MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 = -1000
    return;
}
```

So the actual modulation applied at spawn time can reach **-10%**, but the pre-check only guards against **-5%**.

**Exploit path:**

1. Neuron owner accumulates maturity `M` in the range `[min_stake / 0.95, min_stake / 0.90)`. With `min_stake = 100_000_000 e8s` (1 ICP), this is approximately `[105_263_158, 111_111_111)` e8s.
2. The owner waits for or observes the maturity modulation reaching `-1000` permyriad (−10%), which occurs when the 7-day ICP price average is significantly below the 365-day average.
3. The owner calls `spawn_neuron`. The pre-check computes `M * 0.95 >= 100_000_000` → passes.
4. `maybe_spawn_neurons` applies `apply_maturity_modulation(M, -1000)` = `M * 9000 / 10000` < `100_000_000`.
5. A child neuron is minted with `cached_neuron_stake_e8s` below `neuron_minimum_stake_e8s`.

### Impact Explanation

A neuron with below-minimum stake is created on the ICP ledger. This violates the protocol invariant that all neurons must meet the minimum stake requirement. Such a neuron may be unable to participate in governance operations that enforce the minimum stake, and its existence represents an inconsistent state that could affect downstream logic (e.g., merge operations, stake checks). The ICP is minted and transferred, so there is no fund loss to the protocol, but the neuron owner obtains a governance object that should not exist under the protocol rules.

### Likelihood Explanation

The attacker is any neuron owner — no privileged access is required. The only external dependency is the ICP market price reaching a level where the 7-day average is sufficiently below the 365-day average to push modulation to -10%. This is a realistic market condition (e.g., a sustained price decline). The neuron owner can monitor the `get_maturity_modulation` query endpoint to observe when the modulation is near -1000 permyriad and time the `spawn_neuron` call accordingly.

### Recommendation

Replace the hardcoded `-0.05` in the spawn pre-check with the actual Mission 70 minimum modulation constant:

```rust
// Use the actual worst-case modulation bound, not the stale -5%
let worst_case_modulation_factor =
    1.0 - (MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70.unsigned_abs() as f64 / 10_000.0);
let least_possible_stake = (maturity_to_spawn as f64 * worst_case_modulation_factor) as u64;
```

Alternatively, perform the check using integer arithmetic consistent with `apply_maturity_modulation`:

```rust
let least_possible_stake = apply_maturity_modulation(
    maturity_to_spawn,
    MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as i32,
).unwrap_or(0);
```

### Proof of Concept

**Numeric example** with `neuron_minimum_stake_e8s = 100_000_000`:

| Variable | Value |
|---|---|
| `maturity_to_spawn` | `108_000_000 e8s` (~1.08 ICP) |
| Pre-check: `108_000_000 * 0.95` | `102_600_000` ≥ `100_000_000` → **passes** |
| Actual stake at -10%: `108_000_000 * 9000 / 10000` | `97_200_000` < `100_000_000` → **below minimum** |

**Code references:**

- Pre-check (stale -5%): [1](#0-0) 
- Mission 70 minimum bound (-10%): [2](#0-1) 
- Actual modulation applied at spawn: [3](#0-2) 
- Valid range enforcement in `maybe_spawn_neurons`: [4](#0-3) 
- `apply_maturity_modulation` (integer arithmetic, truncates): [5](#0-4) 
- CHANGELOG confirming Mission 70 spawning now uses the new modulation source: [6](#0-5)

### Citations

**File:** rs/nns/governance/src/governance.rs (L2666-2673)
```rust
        let least_possible_stake = (maturity_to_spawn as f64 * (1_f64 - 0.05)) as u64;

        if least_possible_stake < economics.neuron_minimum_stake_e8s {
            return Err(GovernanceError::new_with_message(
                ErrorType::InsufficientFunds,
                "There isn't enough maturity to spawn a new neuron due to worst case maturity modulation.",
            ));
        }
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

**File:** rs/nns/governance/CHANGELOG.md (L14-23)
```markdown
# 2026-05-17: Proposal 141779

http://dashboard.internetcomputer.org/proposal/141779

## Changed

* Neuron spawning and maturity disbursement finalization now read the locally
  computed Mission 70 maturity modulation (derived from the XRC-backed price
  history) instead of the CMC-polled `cached_daily_maturity_modulation_basis_points`.

```
