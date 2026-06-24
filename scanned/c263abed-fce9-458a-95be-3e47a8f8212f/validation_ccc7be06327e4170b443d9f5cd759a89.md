### Title
Inconsistent Maturity Modulation Methodology Between `spawn_neuron` Validation and `maybe_spawn_neurons` Execution - (File: rs/nns/governance/src/governance.rs)

### Summary

The NNS Governance canister uses two different maturity modulation methodologies in different code paths: `spawn_neuron` validates against a hardcoded 5% worst-case (matching the old CMC-based ±500 permyriad bounds), while `maybe_spawn_neurons` and `finalize_maturity_disbursement` apply the new Mission 70 locally-computed modulation with ±200 permyriad (±2%) bounds. This inconsistency causes users to be incorrectly denied spawning neurons when their maturity falls in the 2%–5% gap zone.

### Finding Description

**Validation path (`spawn_neuron`)** uses a hardcoded 5% worst-case floor:

```rust
let least_possible_stake = (maturity_to_spawn as f64 * (1_f64 - 0.05)) as u64;
if least_possible_stake < economics.neuron_minimum_stake_e8s {
    return Err(GovernanceError::new_with_message(
        ErrorType::InsufficientFunds,
        "There isn't enough maturity to spawn a new neuron due to worst case maturity modulation.",
    ));
}
``` [1](#0-0) 

This 5% figure matches the old CMC-based bounds (`MIN_MATURITY_MODULATION_PERMYRIAD = -500`): [2](#0-1) 

**Execution path (`maybe_spawn_neurons`)** reads the Mission 70 locally-computed modulation, which has ±200 permyriad (±2%) bounds:

```rust
let maturity_modulation = match self
    .heap_data
    .maturity_modulation
    .as_ref()
    .and_then(|m| m.current_value_permyriad)
{ ... }
if !VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE.contains(&maturity_modulation) { ... }
``` [3](#0-2) 

The Mission 70 algorithm computes modulation with a tighter global bound (`MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70 = 200`): [4](#0-3) 

The CHANGELOG confirms the switchover happened in Proposal 141779:

> "Neuron spawning and maturity disbursement finalization now read the locally computed Mission 70 maturity modulation … instead of the CMC-polled `cached_daily_maturity_modulation_basis_points`." [5](#0-4) 

**`finalize_maturity_disbursement`** also uses Mission 70 modulation (not the 5% floor), confirming the execution side is consistently on the new methodology: [6](#0-5) 

The proto documentation still states the range is [95%, 105%], which is now stale: [7](#0-6) 

### Impact Explanation

Any neuron holder whose `maturity_to_spawn` satisfies:

```
maturity * 0.95 < neuron_minimum_stake_e8s   ← fails spawn_neuron validation (5% floor)
maturity * 0.98 >= neuron_minimum_stake_e8s  ← would succeed under Mission 70 (2% floor)
```

is incorrectly rejected with `InsufficientFunds`. The user loses the ability to spawn a neuron they are legitimately entitled to spawn. The error message ("worst case maturity modulation") is factually incorrect because the actual worst case is now ±2%, not ±5%. This is a governance accounting bug with direct financial impact on neuron holders.

### Likelihood Explanation

The condition is reachable by any unprivileged neuron holder calling `manage_neuron` with a `Spawn` command. It is triggered whenever `maturity_to_spawn` is in the 2%–5% gap relative to `neuron_minimum_stake_e8s`. Given that `neuron_minimum_stake_e8s` is a fixed protocol parameter and maturity accumulates continuously, users near the boundary will routinely hit this. The entry path requires no special privileges.

### Recommendation

Update the worst-case floor in `spawn_neuron` to match the Mission 70 bounds (`MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 = -200`, i.e., 2%):

```rust
// Replace:
let least_possible_stake = (maturity_to_spawn as f64 * (1_f64 - 0.05)) as u64;
// With:
let least_possible_stake = apply_maturity_modulation(
    maturity_to_spawn,
    MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as i32,
).unwrap_or(0);
```

Also update the proto comment for `DisburseMaturity` to reflect the actual [98%, 102%] range.

### Proof of Concept

1. Assume `neuron_minimum_stake_e8s = 100_000_000` (1 ICP).
2. User has `maturity_to_spawn = 102_040_817` e8s (~1.02 ICP).
3. Validation: `102_040_817 * 0.95 = 96_938_776 < 100_000_000` → **rejected** with `InsufficientFunds`.
4. Actual Mission 70 worst case: `102_040_817 * 0.98 = 99_999_999 ≈ 100_000_000` → **would succeed**.
5. The user is denied a valid spawn. The error message falsely claims worst-case modulation would produce insufficient stake, when in reality the actual worst case (±2%) would produce exactly the minimum.

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

**File:** rs/nervous_system/governance/src/maturity_modulation/mod.rs (L4-5)
```rust
pub const MIN_MATURITY_MODULATION_PERMYRIAD: i32 = -500;
pub const MAX_MATURITY_MODULATION_PERMYRIAD: i32 = 500;
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

**File:** rs/nns/governance/CHANGELOG.md (L14-22)
```markdown
# 2026-05-17: Proposal 141779

http://dashboard.internetcomputer.org/proposal/141779

## Changed

* Neuron spawning and maturity disbursement finalization now read the locally
  computed Mission 70 maturity modulation (derived from the XRC-backed price
  history) instead of the CMC-polled `cached_daily_maturity_modulation_basis_points`.
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L451-512)
```rust
fn next_maturity_disbursement_to_finalize(
    neuron_store: &NeuronStore,
    in_flight_commands: &HashMap<u64, NeuronInFlightCommand>,
    maturity_modulation_basis_points: Option<i32>,
    now_seconds: u64,
) -> Result<Option<MaturityDisbursementFinalization>, FinalizeMaturityDisbursementError> {
    let maturity_modulation_basis_points = maturity_modulation_basis_points
        .ok_or(FinalizeMaturityDisbursementError::NoMaturityModulation)?;

    // Try to find the first neuron eligible for finalizing maturity disbursement, that is not
    // locked.
    let Some(neuron_id) = neuron_store
        .get_neuron_ids_ready_to_finalize_maturity_disbursement(now_seconds)
        .into_iter()
        .find(|neuron_id| !in_flight_commands.contains_key(&neuron_id.id))
    else {
        // If all neurons are locked, we don't need to finalize anything.
        return Ok(None);
    };
    // Either of the errors below indicates a bug in the maturity disbursement index.
    let maturity_disbursement_in_progress = neuron_store
        .with_neuron(&neuron_id, |neuron| {
            neuron.maturity_disbursements_in_progress().first().cloned()
        })
        .map_err(|_| FinalizeMaturityDisbursementError::NeuronNotFound(neuron_id))?
        .ok_or(FinalizeMaturityDisbursementError::NoMaturityDisbursement(
            neuron_id,
        ))?;

    let MaturityDisbursement {
        amount_e8s: original_maturity_e8s_equivalent,
        destination,
        finalize_disbursement_timestamp_seconds,
        timestamp_of_disbursement_seconds: _,
    } = maturity_disbursement_in_progress;

    // Make sure the disbursement is ready to be finalized. Failure at this step probably means the
    // maturity disbursement index is wrong.
    if now_seconds < finalize_disbursement_timestamp_seconds {
        return Err(
            FinalizeMaturityDisbursementError::NotTimeToFinalizeMaturityDisbursement {
                neuron_id,
                finalize_disbursement_timestamp_seconds,
                now_seconds,
            },
        );
    }

    // Apply the maturity modulation to the disbursement amount. This should not fail unless
    // something else in the system is wrong, such as an insanely large amount of maturity or an
    // incorrect maturity modulation basis points.
    let maturity_to_disburse_after_modulation_e8s = apply_maturity_modulation(
        original_maturity_e8s_equivalent,
        maturity_modulation_basis_points,
    )
    .map_err(
        |reason| FinalizeMaturityDisbursementError::MaturityModulationFailure {
            maturity_before_modulation_e8s: original_maturity_e8s_equivalent,
            maturity_modulation_basis_points,
            reason,
        },
    )?;
```

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L973-978)
```text
  // Disburse the maturity of a neuron to any ledger account. If an account is not specified, the
  // controller's account will be used. The controller can choose a percentage of the current
  // maturity to disburse to the ledger account. The resulting amount to disburse must be at least 1
  // ICP. The disbursement has a 7-day delay before it is finalized. At the finalization time, the
  // maturity modulation will be applied to the amount, which can make the amount [95%, 105%] of the
  // original amount.
```
