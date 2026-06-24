### Title
Stale Hardcoded Worst-Case Slippage in `spawn_neuron` Allows Minting Below Minimum Stake Under Mission 70 Modulation Range - (File: `rs/nns/governance/src/governance.rs`)

---

### Summary

`spawn_neuron` in NNS Governance validates the minimum spawnable stake using a hardcoded 5% worst-case maturity modulation, but the actual modulation range under Mission 70 extends to −10%. A neuron controller can successfully call `spawn_neuron` with maturity that passes the stale 5% guard, yet when `maybe_spawn_neurons` later mints the ICP using the real modulation value, the resulting `cached_neuron_stake_e8s` falls below `neuron_minimum_stake_e8s`, violating the protocol invariant.

---

### Finding Description

In `spawn_neuron`, the pre-flight check computes the worst-case stake as:

```rust
let least_possible_stake = (maturity_to_spawn as f64 * (1_f64 - 0.05)) as u64;
``` [1](#0-0) 

The literal `0.05` encodes the old ±5% modulation range defined in `rs/nervous_system/governance/src/maturity_modulation/mod.rs`:

```rust
pub const MIN_MATURITY_MODULATION_PERMYRIAD: i32 = -500;  // −5%
pub const MAX_MATURITY_MODULATION_PERMYRIAD: i32 = 500;
``` [2](#0-1) 

However, Mission 70 introduced a new, wider range:

```rust
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000;  // −10%
pub(crate) const MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70: i64 = 200;
``` [3](#0-2) 

`maybe_spawn_neurons` enforces the Mission 70 range at runtime:

```rust
const VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE: RangeInclusive<i32> =
    MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as i32
        ..=MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70 as i32;
``` [4](#0-3) 

and then applies the real modulation without any minimum-output floor:

```rust
let neuron_stake: u64 = match apply_maturity_modulation(
    original_maturity,
    maturity_modulation,
) {
``` [5](#0-4) 

The gap: `spawn_neuron` guards against −5% but `maybe_spawn_neurons` can apply −10%, so any maturity in the band where `maturity × 0.95 ≥ neuron_minimum_stake_e8s` but `maturity × 0.90 < neuron_minimum_stake_e8s` passes the pre-flight check yet produces a sub-minimum stake at mint time.

---

### Impact Explanation

The minted `cached_neuron_stake_e8s` of the child neuron falls below `neuron_minimum_stake_e8s`. This breaks the protocol invariant that every neuron holds at least the minimum stake. The child neuron is permanently recorded on the ICP ledger with an under-minimum balance, and the parent neuron's maturity has already been zeroed out (`neuron.maturity_e8s_equivalent = 0` at line 6515), so the ICP is irreversibly minted at the wrong amount. This is a ledger conservation / governance accounting bug: the conversion from maturity to ICP stake uses an incorrect minimum-output bound, directly analogous to `amountOutMin = 0` in the original report. [6](#0-5) 

---

### Likelihood Explanation

**Medium.** The condition requires two things to be true simultaneously:

1. Mission 70 is active (already deployed on mainnet per the governance constants).
2. The daily maturity modulation value falls in the −5% to −10% band. The new algorithm (`compute_maturity_modulation_permyriad`) can reach −10% when the 7-day ICP price average is sufficiently below the 365-day reference average. [7](#0-6) 

Any NNS neuron controller can trigger this by calling `spawn_neuron` during a period of ICP price decline. No privileged access, no threshold corruption, and no social engineering is required beyond controlling a neuron with sufficient maturity.

---

### Recommendation

Replace the hardcoded `0.05` literal in `spawn_neuron` with a value derived from the actual enforced minimum modulation constant:

```rust
// Use the real worst-case modulation bound, not a stale hardcoded 5%.
let worst_case_modulation = apply_maturity_modulation(
    maturity_to_spawn,
    MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as i32,
).unwrap_or(0);

if worst_case_modulation < economics.neuron_minimum_stake_e8s {
    return Err(...);
}
```

This mirrors the correct pattern already used in SNS governance's `disburse_maturity`:

```rust
let worst_case_maturity_modulation =
    apply_maturity_modulation(maturity_to_deduct, MIN_MATURITY_MODULATION_PERMYRIAD)
``` [8](#0-7) 

The NNS `spawn_neuron` should adopt the same pattern, referencing `MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70` instead of the old constant or a bare float literal.

---

### Proof of Concept

**Setup**: Mission 70 is active. `neuron_minimum_stake_e8s = 100_000_000` (1 ICP). Daily modulation = −800 permyriad (−8%), which is within the valid Mission 70 range of [−1000, 200].

**Step 1** — Neuron controller calls `spawn_neuron` with `maturity_to_spawn = 106_000_000` e8s (≈1.06 ICP):

```
least_possible_stake = 106_000_000 * (1 - 0.05) = 100_700_000
100_700_000 >= 100_000_000  → check PASSES
```

Child neuron is created in spawning state with `maturity_e8s_equivalent = 106_000_000`. [9](#0-8) 

**Step 2** — Timer fires `maybe_spawn_neurons`. Modulation = −800 permyriad:

```
neuron_stake = apply_maturity_modulation(106_000_000, -800)
             = 106_000_000 * (10_000 - 800) / 10_000
             = 106_000_000 * 0.92
             = 97_520_000
```

`97_520_000 < 100_000_000 = neuron_minimum_stake_e8s`

The ICP ledger mints 97_520_000 e8s into the child neuron's subaccount. The parent's maturity is already zeroed. The child neuron permanently holds a sub-minimum stake, violating the protocol invariant, with no recourse. [10](#0-9) [11](#0-10)

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

**File:** rs/nns/governance/src/governance.rs (L6509-6517)
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

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L47-50)
```rust
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000;

/// Upper bound for Mission 70 maturity modulation: +2% = 200 permyriad.
pub(crate) const MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70: i64 = 200;
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L130-196)
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
