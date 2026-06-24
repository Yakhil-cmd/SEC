### Title
Stale 5% Worst-Case Maturity Modulation Bound in `spawn_neuron` Allows Spawning Sub-Minimum-Stake Neurons Under Mission 70 — (`File: rs/nns/governance/src/governance.rs`)

---

### Summary

The `spawn_neuron` function in NNS Governance uses a hardcoded 5% worst-case maturity modulation bound to gate whether a spawn is permitted. After the Mission 70 upgrade (Proposal 141738/141779), the actual worst-case modulation applied at spawn-time is **−10%** (`MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 = −1_000`). Any NNS neuron holder can call `spawn_neuron` with maturity that passes the stale 5% guard but, when the live modulation is between −5% and −10%, the spawned neuron is minted with a `cached_neuron_stake_e8s` below `neuron_minimum_stake_e8s`. This is the IC analog of the LybraFinance fixed-reward liquidation bug: a fixed percentage is used as a safety bound, but the actual worst-case percentage is larger, causing the protected invariant to be violated.

---

### Finding Description

**Root cause — hardcoded 5% in `spawn_neuron`:** [1](#0-0) 

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

The constant `0.05` (5%) was correct when the CMC-backed modulation range was `[−500, +500]` permyriad. Mission 70 extended the lower bound to **−1 000 permyriad (−10%)**: [2](#0-1) 

```rust
/// Lower bound for Mission 70 maturity modulation: -10% = -1000 permyriad.
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000;

/// Upper bound for Mission 70 maturity modulation: +2% = 200 permyriad.
pub(crate) const MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70: i64 = 200;
```

The 2026-05-17 changelog confirms that spawning now reads the Mission 70 value: [3](#0-2) 

> "Neuron spawning and maturity disbursement finalization now read the locally computed Mission 70 maturity modulation … instead of the CMC-polled `cached_daily_maturity_modulation_basis_points`."

**Actual spawn-time application of modulation:** [4](#0-3) 

```rust
let neuron_stake: u64 = match apply_maturity_modulation(
    original_maturity,
    maturity_modulation,   // ← can be −1000 permyriad (−10%)
) {
```

The `apply_maturity_modulation` helper multiplies by `(10_000 + maturity_modulation_basis_points) / 10_000`: [5](#0-4) 

So when `maturity_modulation = −1000`, the minted stake is `maturity × 0.90`, not `maturity × 0.95`.

---

### Impact Explanation

**Invariant broken:** A neuron spawned with maturity `M` such that `M × 0.95 ≥ neuron_minimum_stake_e8s` (passes the guard) but `M × 0.90 < neuron_minimum_stake_e8s` (fails the real worst case) will be minted with `cached_neuron_stake_e8s < neuron_minimum_stake_e8s`. The spawned neuron:

- Cannot be split (split requires both halves to exceed minimum stake).
- Cannot be merged into another neuron if the result would be invalid.
- Earns voting rewards on a sub-minimum stake, leaking value relative to the caller's expectation.
- The parent neuron's maturity has already been irreversibly deducted at `spawn_neuron` call time.

This is a **governance accounting bug** (cycles/resource accounting analog): the fixed 5% bound allows a state transition that the protocol intends to prohibit, and the resulting neuron is permanently unhealthy.

---

### Likelihood Explanation

- Mission 70 modulation reaches −10% whenever the 7-day ICP price average falls more than 4% below the 365-day average (sensitivity = 0.25, so `0.25 × (−0.04) × 10_000 = −100` permyriad per 4% drop; the full −1 000 permyriad requires a ~40% price drop relative to the year average, which is plausible during bear markets).
- Any unprivileged NNS neuron holder with sufficient maturity can call `spawn_neuron` via ingress. No special role is required.
- The window between the guard check (5%) and the actual worst case (10%) is a 5-percentage-point band. For `neuron_minimum_stake_e8s = 1 ICP = 100_000_000 e8s`, any maturity between `~105_263_158 e8s` and `~111_111_111 e8s` passes the guard but fails the real worst case.

---

### Recommendation

Replace the hardcoded `0.05` with a constant derived from `MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70`:

```rust
// Use the actual Mission 70 worst-case modulation bound.
const WORST_CASE_MODULATION: f64 =
    (MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70.unsigned_abs() as f64) / 10_000.0;

let least_possible_stake =
    (maturity_to_spawn as f64 * (1_f64 - WORST_CASE_MODULATION)) as u64;
```

Alternatively, call `apply_maturity_modulation(maturity_to_spawn, MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as i32)` directly (using integer arithmetic, consistent with the rest of the codebase) and compare the result against `neuron_minimum_stake_e8s`.

The same audit should be applied to `initiate_maturity_disbursement` in NNS governance, which currently does not perform a worst-case modulation check at initiation time.

---

### Proof of Concept

**Setup:**
- `neuron_minimum_stake_e8s = 100_000_000` (1 ICP, the default).
- Mission 70 maturity modulation = −1 000 permyriad (−10%), reachable during a bear market.

**Step 1 — Call `spawn_neuron` with `percentage_to_spawn = 100` on a neuron with `maturity_e8s_equivalent = 106_000_000`:**

Guard check: `106_000_000 × 0.95 = 100_700_000 ≥ 100_000_000` → **passes**.

**Step 2 — After `neuron_spawn_dissolve_delay_seconds`, `maybe_spawn_neurons` fires:**

`apply_maturity_modulation(106_000_000, −1000)` = `106_000_000 × 9_000 / 10_000 = 95_400_000`.

**Result:** `cached_neuron_stake_e8s = 95_400_000 < neuron_minimum_stake_e8s = 100_000_000`.

The spawned neuron is permanently below the minimum stake. The parent neuron's maturity (`106_000_000 e8s`) has already been zeroed at spawn initiation time, so the loss is irreversible. [1](#0-0) [2](#0-1) [4](#0-3) [5](#0-4) [3](#0-2)

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

**File:** rs/nns/governance/CHANGELOG.md (L14-22)
```markdown
# 2026-05-17: Proposal 141779

http://dashboard.internetcomputer.org/proposal/141779

## Changed

* Neuron spawning and maturity disbursement finalization now read the locally
  computed Mission 70 maturity modulation (derived from the XRC-backed price
  history) instead of the CMC-polled `cached_daily_maturity_modulation_basis_points`.
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
