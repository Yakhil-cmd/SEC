### Title
Proportional Speed-Limit Multiplier After XRC Downtime Allows Sudden Maturity Modulation Jump Without Grace Period - (`rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs`)

---

### Summary

When the Exchange Rate Canister (XRC) is unavailable for N consecutive days and then resumes, the NNS Governance canister's backfill mechanism fetches all missing days and then applies a single maturity modulation update. The `days_elapsed` multiplier in `compute_maturity_modulation_permyriad` scales the daily speed limit by N, effectively removing the speed limit for N ≥ 34 days. Neuron owners with pending maturity disbursements (7-day delay) or neurons in spawning state cannot cancel them and have no grace period to react to the sudden jump.

---

### Finding Description

The `UpdateIcpXdrRateRelatedData` timer task in `rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs` fetches ICP/XDR rates from the XRC daily and maintains a 365-day price history used to compute maturity modulation. When XRC is unavailable, the task fails to fetch rates and the modulation is not updated.

When XRC resumes, the backfill mechanism fires every `BACKFILL_INTERVAL_SECONDS = 5` seconds to fetch all missing days: [1](#0-0) 

After all missing days are fetched, `update_maturity_modulation` is called once for `current_day`. Inside `compute_maturity_modulation_permyriad`, the speed limit is scaled by `days_elapsed`: [2](#0-1) 

The daily speed limit is 30 permyriad: [3](#0-2) 

For N days of XRC downtime, `max_change = N × 30 permyriad`. At N ≥ 34, `max_change ≥ 1020 > 1000`, which exceeds the global lower bound of −1000 permyriad. The speed limit is effectively nullified: the modulation can jump from any prior value to the extreme of [−1000, +200] permyriad in a single update.

This modulation is immediately consumed by neuron spawning: [4](#0-3) 

And by maturity disbursement finalization: [5](#0-4) 

The `VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE` used in the spawning guard is defined as: [6](#0-5) 

This range is `[MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70, MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70]` = [−1000, +200], so a post-backfill value of −1000 passes the sanity check and is applied immediately.

There is no mechanism to cancel a pending `DisburseMaturity` (7-day delay) or a neuron already in spawning state. The neuron owner has no grace period to react.

---

### Impact Explanation

A neuron owner who initiates a maturity disbursement (7-day finalization delay) or a neuron spawn during a period when XRC is unavailable will have the maturity modulation applied at finalization time — which may be the first moment after XRC resumes and the backfill completes. If the ICP price moved significantly during the downtime, the modulation can jump by up to 10% (−1000 permyriad) in a single update, reducing the ICP received from disbursement by up to 10% relative to what the owner expected based on the pre-downtime modulation. The owner cannot cancel the pending operation. This is the direct IC analog of M-2: users cannot react to price changes while the feed is down, and when it resumes, the full accumulated change is applied at once.

---

### Likelihood Explanation

XRC subnet unavailability is an explicitly documented operational scenario. The upgrade script for the CMC notes:

> "When CMC is recovered from mainnet, it soon starts making calls to the Exchange Rate Canister (XRC), which is on a subnet that is not recovered." [7](#0-6) 

This confirms that XRC subnet downtime lasting multiple days is a known, non-hypothetical event. The backfill mechanism is explicitly designed to recover from such gaps, and the `days_elapsed` multiplier is the code path that removes the speed limit during recovery.

---

### Recommendation

1. **Cap `days_elapsed` at 1** in `compute_maturity_modulation_permyriad` so the speed limit is never scaled beyond a single day's allowance, regardless of how long XRC was unavailable. The backfill already fills in historical prices; the speed limit should still apply to the single daily modulation update.

2. **Alternatively**, after a gap of more than a configurable threshold (e.g., 7 days), treat the resumed update as a "first calculation" (i.e., set `previous = None`) so the modulation jumps directly to the target without the proportional multiplier — but add a grace period (e.g., 24 hours) before pending disbursements are finalized, analogous to Chainlink's recommended sequencer grace period.

3. **Expose a cancellation path** for pending `DisburseMaturity` operations so neuron owners can react to a sudden modulation change before finalization.

---

### Proof of Concept

1. Neuron owner initiates `DisburseMaturity` with 100% of maturity (e.g., 100 ICP worth). Finalization is scheduled 7 days later. Maturity modulation is currently 0 permyriad.
2. XRC subnet goes down for 34 days. `UpdateIcpXdrRateRelatedData` fails to fetch rates; modulation is not updated (`updated_at_days_since_epoch` stays at day D).
3. XRC resumes. Backfill fetches 34 missing days at 5-second intervals (~3 minutes total). ICP price has dropped 15% during the outage.
4. `update_maturity_modulation` is called with `current_day = D + 34`, `previous_day = D`, `days_elapsed = 34`.
5. `max_change = 34 × 30 = 1020 permyriad`. The target modulation (driven by the 15% price drop) is approximately −375 permyriad. Since `max_change = 1020 > 375`, the speed limit does not constrain the jump. Modulation updates from 0 to −375 permyriad in one step.
6. The pending disbursement is finalized immediately after the backfill completes. The neuron owner receives `100 × (1 − 375/10000) = 96.25 ICP` instead of the ~100 ICP they expected. They had no opportunity to cancel.
7. In a more extreme scenario (ICP price crashed 50% over 34 days), the target modulation hits the global floor of −1000 permyriad, and the owner receives only 90 ICP — a 10% loss with no recourse.

### Citations

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L43-50)
```rust
/// Maximum daily change in maturity modulation: 0.3% = 30 permyriad.
const MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD: i64 = 30;

/// Lower bound for Mission 70 maturity modulation: -10% = -1000 permyriad.
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000;

/// Upper bound for Mission 70 maturity modulation: +2% = 200 permyriad.
pub(crate) const MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70: i64 = 200;
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L52-54)
```rust
/// Delay between consecutive XRC calls while backfilling historical rates. At 5 seconds per call,
/// filling the full 365-day window takes about 30 minutes.
const BACKFILL_INTERVAL_SECONDS: u64 = 5;
```

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L165-188)
```rust
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
```

**File:** rs/nns/governance/src/governance.rs (L276-278)
```rust
const VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE: RangeInclusive<i32> =
    MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as i32
        ..=MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70 as i32;
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

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L499-512)
```rust
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

**File:** rs/nervous_system/tools/release/upgrade-canister-to-working-tree.sh (L39-47)
```shellscript
# When CMC is recovered from mainnet, it soon starts making calls to the Exchange Rate Canister (XRC), which is on a
# subnet that is not recovered.  These calls don't timeout and can't return.  That prevents CMC from ever being able to
# stop, which means we could never complete the upgrade. However, because they cannot return,
# it is safe to skip stopping when testing the upgrade of this canister, as replies cannot cause arbitrary code to execute
# when they return (which is the only reason for stopping in the first place).  The upgrade will still work, and
# the upgrade process will be exercised.
if [ "$CANISTER_NAME" == "cycles-minting" ]; then
    export SKIP_STOPPING=yes
fi
```
