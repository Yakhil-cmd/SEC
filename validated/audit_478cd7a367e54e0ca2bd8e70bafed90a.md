Audit Report

## Title
`spawn_neuron` Hardcoded 5% Worst-Case Guard Understates Mission 70's -10% Lower Bound, Allowing Sub-Minimum-Stake Neuron Creation — (`rs/nns/governance/src/governance.rs`)

## Summary
The `spawn_neuron` function guards against sub-minimum-stake child neurons by computing `maturity_to_spawn * 0.95`, assuming a worst-case 5% maturity modulation reduction. However, the Mission 70 modulation lower bound is -10% (`MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 = -1_000`), and `maybe_spawn_neurons` validates and applies modulation values down to -1000 permyriad via `VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE`. Any `maturity_to_spawn` in the range `[neuron_minimum_stake_e8s / 0.95, neuron_minimum_stake_e8s / 0.90)` passes the guard but results in a minted neuron stake below `neuron_minimum_stake_e8s`, with the difference permanently lost.

## Finding Description
**Root cause — hardcoded 5% in `spawn_neuron`:** [1](#0-0) 

The guard uses `1_f64 - 0.05` (5% reduction) as the worst case. This was correct under the original ±5% modulation bounds defined in `rs/nervous_system/governance/src/maturity_modulation/mod.rs` (`MIN_MATURITY_MODULATION_PERMYRIAD = -500`), but Mission 70 introduced an asymmetric range: [2](#0-1) 

The NNS governance `VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE` was updated to use these Mission 70 constants: [3](#0-2) 

**Exploit path in `maybe_spawn_neurons`:**

`maturity_modulation` is read from `heap_data.maturity_modulation.current_value_permyriad`, validated to be within `VALID_MATURITY_MODULATION_BASIS_POINTS_RANGE` (which permits -1000), and then passed directly to `apply_maturity_modulation`: [4](#0-3) [5](#0-4) 

`apply_maturity_modulation` enforces no bounds on its input — it computes `amount * (10_000 + basis_points) / 10_000` for any `i32`: [6](#0-5) 

When `basis_points = -1000`, the result is `amount * 9000 / 10000 = amount * 0.90`. The guard in `spawn_neuron` only rejected values where `amount * 0.95 < neuron_minimum_stake_e8s`, so any `maturity_to_spawn` satisfying `neuron_minimum_stake_e8s / 0.95 > maturity_to_spawn >= neuron_minimum_stake_e8s / 0.90` passes the guard but produces a sub-minimum minted stake. The maturity is consumed (set to 0) and the child neuron is created on the ledger with `cached_neuron_stake_e8s` below `neuron_minimum_stake_e8s`, with the gap permanently unrecoverable.

## Impact Explanation
A neuron controller permanently loses ICP-equivalent value: their maturity is fully consumed but the minted stake falls below the protocol minimum. The child neuron exists on the ledger with a real ICP balance below `neuron_minimum_stake_e8s` (default 1 ICP), violating the core NNS invariant. Subsequent operations enforcing the minimum stake (split, merge, disburse-to-neuron) will behave inconsistently against this neuron. This constitutes a concrete, permanent, per-transaction loss of user funds caused by a protocol-level accounting error in NNS governance — matching the **Medium** impact tier: meaningful security impact with concrete user and protocol harm, triggerable without special privileges.

## Likelihood Explanation
Mission 70 modulation reaches -10% when the 7-day ICP price average is ~10% below the 365-day average — a realistic condition during sustained ICP price declines. The modulation moves at most 30 permyriad/day (`MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD = 30`), reaching -1000 after ~33 consecutive days of downward movement. [7](#0-6)  Any unprivileged neuron controller can trigger this by calling `spawn_neuron` with a `percentage_to_spawn` placing `maturity_to_spawn` in the vulnerable range. No privileged access is required.

## Recommendation
Replace the hardcoded `0.05` with the actual Mission 70 lower bound, mirroring the pattern already used in SNS `disburse_maturity`:

```rust
// Use apply_maturity_modulation with the actual worst-case bound
let least_possible_stake = apply_maturity_modulation(
    maturity_to_spawn,
    MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as i32,
)
.unwrap_or(0);
```

This directly mirrors the SNS governance pattern: [8](#0-7) 

## Proof of Concept
Assume `neuron_minimum_stake_e8s = 100_000_000` (1 ICP) and Mission 70 modulation is at -1000 permyriad (-10%):

1. Neuron has `maturity_e8s_equivalent = 120_000_000`.
2. Controller calls `spawn_neuron` with `percentage_to_spawn = 89` → `maturity_to_spawn = 106_800_000`.
3. Guard: `106_800_000 * 0.95 = 101_460_000 > 100_000_000` → **passes**.
4. `maybe_spawn_neurons` applies -10%: `apply_maturity_modulation(106_800_000, -1000)` = `106_800_000 * 9000 / 10000 = 96_120_000`.
5. Child neuron minted with `cached_neuron_stake_e8s = 96_120_000` — **below the 100,000,000 minimum**.
6. Parent maturity reduced by 106,800,000 e8s; child receives only 96,120,000 e8s; 10,680,000 e8s permanently lost.

A deterministic integration test can reproduce this by: (a) setting `heap_data.maturity_modulation.current_value_permyriad = -1000`, (b) creating a neuron with `maturity_e8s_equivalent = 106_800_000`, (c) calling `spawn_neuron` with `percentage_to_spawn = 89`, (d) advancing time past `spawn_at_timestamp_seconds`, (e) calling `maybe_spawn_neurons`, and (f) asserting `cached_neuron_stake_e8s < neuron_minimum_stake_e8s`.

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

**File:** rs/nns/governance/src/governance.rs (L6484-6517)
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

                    println!(
                        "{}Spawning neuron: {:?}. Performing ledger update.",
                        LOG_PREFIX, neuron
                    );

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

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L43-44)
```rust
/// Maximum daily change in maturity modulation: 0.3% = 30 permyriad.
const MATURITY_MODULATION_DAILY_SPEED_LIMIT_PERMYRIAD: i64 = 30;
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
