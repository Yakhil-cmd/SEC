### Title
Spawn Neuron Minimum-Stake Guard Uses Stale −5 % Worst-Case While Mission 70 Minimum Modulation Is −10 % — (`rs/nns/governance/src/governance.rs`)

---

### Summary

`spawn_neuron()` validates that a neuron has enough maturity to produce a stake above `neuron_minimum_stake_e8s` by assuming the worst-case maturity modulation is −5 %. However, the Mission 70 maturity modulation system (now live on mainnet) can reach −10 %. When the ICP price is persistently below its 365-day average, the actual modulation applied at minting time can be −10 %, causing the spawned neuron to receive less ICP than the minimum stake — a governance accounting inconsistency reachable by any unprivileged neuron holder.

---

### Finding Description

**Root cause — `spawn_neuron` guard (line 2666):**

```rust
// rs/nns/governance/src/governance.rs
let least_possible_stake = (maturity_to_spawn as f64 * (1_f64 - 0.05)) as u64;

if least_possible_stake < economics.neuron_minimum_stake_e8s {
    return Err(GovernanceError::new_with_message(
        ErrorType::InsufficientFunds,
        "There isn't enough maturity to spawn a new neuron due to worst case maturity modulation.",
    ));
}
```

The constant `0.05` (−5 %) is hardcoded and was correct under the old CMC-polled modulation range (`MIN_MATURITY_MODULATION_PERMYRIAD = -500`). [1](#0-0) 

**New Mission 70 minimum is −10 %:**

```rust
// rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000;  // -10%
pub(crate) const MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70: i64 = 200;     // +2%
``` [2](#0-1) 

**`maybe_spawn_neurons` now reads the Mission 70 modulation:**

```rust
// rs/nns/governance/src/governance.rs
let maturity_modulation = match self
    .heap_data
    .maturity_modulation          // ← Mission 70 field, range [-1000, 200]
    .as_ref()
    .and_then(|m| m.current_value_permyriad)
{
    None => return,
    Some(value) => value,
};
``` [3](#0-2) 

The CHANGELOG confirms the switchover: *"Neuron spawning and maturity disbursement finalization now read the locally computed Mission 70 maturity modulation … instead of the CMC-polled `cached_daily_maturity_modulation_basis_points`."* [4](#0-3) 

**`apply_maturity_modulation` at minting time:**

```rust
// rs/nervous_system/governance/src/maturity_modulation/mod.rs
pub fn apply_maturity_modulation(
    amount_maturity_e8s: u64,
    maturity_modulation_basis_points: i32,  // can be -1000 (Mission 70 min)
) -> Result<u64, String> { ... }
``` [5](#0-4) 

**The gap:** If a user spawns a neuron with maturity `M` such that `M × 0.95 ≥ min_stake > M × 0.90`, the guard at spawn time passes (−5 % check), but when `maybe_spawn_neurons` fires with modulation = −1000 permyriad (−10 %), the minted ICP = `M × 0.90 < min_stake`. The spawned neuron ends up with a stake below `neuron_minimum_stake_e8s`.

---

### Impact Explanation

- Neurons are created on-chain with a stake below the governance minimum, incrementing `neurons_with_invalid_stake_count` in cached metrics.
- Such neurons may be ineligible to vote or participate in governance proposals, silently reducing the effective voting pool.
- The ICP is not destroyed (it can be dissolved and retrieved), but the governance accounting invariant — that every spawned neuron has at least `neuron_minimum_stake_e8s` — is violated.
- This is analogous to the external report's LP supply inflation: the "margin" (−5 % guard) is insufficient to account for the actual downward price movement (−10 % modulation), causing the system to accept spawns that produce under-collateralised neurons.

---

### Likelihood Explanation

- **Trigger condition 1**: ICP price must be persistently below its 365-day moving average so that `compute_maturity_modulation_permyriad` drives the modulation toward −1000 permyriad. This is a realistic market condition (bear market). [6](#0-5) 
- **Trigger condition 2**: A neuron holder must call `spawn_neuron` with maturity in the narrow range `[min_stake / 0.90, min_stake / 0.95)`. With `neuron_minimum_stake_e8s = 100_000_000` e8s (1 ICP), this window is roughly 1.00–1.11 ICP of maturity — easily reachable by any active voter.
- **Entry path**: `spawn_neuron` is a standard unprivileged `manage_neuron` ingress call available to any neuron controller. No special role or key is required.

---

### Recommendation

Replace the hardcoded `0.05` in `spawn_neuron` with the actual Mission 70 minimum modulation constant:

```rust
// Use the real worst-case modulation instead of the stale -5% constant
let worst_case_modulation = MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70; // -1000
let least_possible_stake = apply_maturity_modulation(
    maturity_to_spawn,
    worst_case_modulation as i32,
)?;
```

This ensures the guard at spawn time is consistent with the modulation that will actually be applied at minting time, preventing neurons with below-minimum stakes from being created.

---

### Proof of Concept

1. Assume `neuron_minimum_stake_e8s = 100_000_000` (1 ICP).
2. User calls `spawn_neuron` with `maturity_to_spawn = 106_000_000` e8s (~1.06 ICP).
3. Guard check: `106_000_000 × 0.95 = 100_700_000 ≥ 100_000_000` → **passes**.
4. Neuron enters spawning state. After 7 days, `maybe_spawn_neurons` fires.
5. Mission 70 modulation is −1000 permyriad (ICP price in sustained bear market).
6. `apply_maturity_modulation(106_000_000, -1000)` = `106_000_000 × (10_000 − 1_000) / 10_000` = `95_400_000` e8s.
7. Spawned neuron stake = `95_400_000 < 100_000_000 = neuron_minimum_stake_e8s`. [7](#0-6) 
8. The neuron is created on-chain with an invalid (below-minimum) stake, violating the governance invariant enforced by `neurons_with_invalid_stake_count`. [8](#0-7)

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

**File:** rs/nns/governance/src/governance.rs (L6427-6447)
```rust
        let maturity_modulation = match self
            .heap_data
            .maturity_modulation
            .as_ref()
            .and_then(|m| m.current_value_permyriad)
        {
            None => return,
            Some(value) => value,
        };

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

**File:** rs/nns/governance/src/governance.rs (L6509-6521)
```rust
                    let (staked_neuron_clone, original_spawn_at_timestamp_seconds) = self
                        .with_neuron_mut(&neuron_id, |neuron| {
                            // Reset the neuron's maturity and set that it's spawning before we actually mint
                            // the stake. This is conservative to prevent a neuron having _both_ the stake and
                            // the maturity at any point in time.
                            let original_spawn_ts = neuron.spawn_at_timestamp_seconds;
                            neuron.maturity_e8s_equivalent = 0;
                            neuron.spawn_at_timestamp_seconds = None;
                            neuron.cached_neuron_stake_e8s = neuron_stake;

                            (neuron.clone(), original_spawn_ts)
                        })
                        .unwrap();
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L46-50)
```rust
/// Lower bound for Mission 70 maturity modulation: -10% = -1000 permyriad.
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000;

/// Upper bound for Mission 70 maturity modulation: +2% = 200 permyriad.
pub(crate) const MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70: i64 = 200;
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L130-197)
```rust
fn compute_maturity_modulation_permyriad(
    rates: &[SampledPrice],
    current_day: u64,
    previous: Option<(i64, u64)>,
) -> Result<i64, String> {
    let recent_icp_price = compute_average_icp_xdr_rate(
        rates,
        current_day,
        MATURITY_MODULATION_CURRENT_ICP_PRICE_WINDOW_DAYS,
    )
    .ok_or_else(|| "no rate available for the recent price window".to_string())?;

    let reference_icp_price = compute_average_icp_xdr_rate(
        rates,
        current_day,
        MATURITY_MODULATION_REFERENCE_ICP_PRICE_WINDOW_DAYS,
    )
    .ok_or_else(|| "no rate available for the reference price window".to_string())?;

    if reference_icp_price == 0 {
        return Err("reference price averaged to zero".to_string());
    }

    let target_modulation = {
        let recent = recent_icp_price as i128;
        let reference = reference_icp_price as i128;
        let sensitivity = MATURITY_MODULATION_SENSITIVITY_PERMYRIAD as i128;
        sensitivity * (recent - reference) / reference
    };

    let speed_limited = match previous {
        // First calculation: no baseline to smooth from, so jump straight to target.
        None => target_modulation,
        Some((previous_permyriad, previous_day)) => {
            // Limit day-to-day change.
            let days_elapsed = current_day.saturating_sub(previous_day);
            let max_change = if days_elapsed > 1 {
                // The timer missed one or more days — allow proportionally more change.
                println!(
                    "{}compute_maturity_modulation_permyriad: {} days elapsed since last update (current_day={}, previous_day={})",
                    LOG_PREFIX, days_elapsed, current_day, previous_day
                );
                MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD.saturating_mul(days_elapsed as i64)
            } else if days_elapsed == 1 {
                MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD
            } else {
                // days_elapsed == 0: either same day or current_day < previous_day (should not happen).
                // Allow at least one day of movement.
                println!(
                    "{}compute_maturity_modulation_permyriad: days_elapsed=0 (current_day={}, previous_day={}); treating as 1 day",
                    LOG_PREFIX, current_day, previous_day
                );
                MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD
            };
            target_modulation.clamp(
                previous_permyriad.saturating_sub(max_change) as i128,
                previous_permyriad.saturating_add(max_change) as i128,
            )
        }
    };

    // Global bounds have final say. The result is within [MIN, MAX] which fit in i64, so the
    // cast is safe.
    Ok(speed_limited.clamp(
        MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as i128,
        MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70 as i128,
    ) as i64)
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
