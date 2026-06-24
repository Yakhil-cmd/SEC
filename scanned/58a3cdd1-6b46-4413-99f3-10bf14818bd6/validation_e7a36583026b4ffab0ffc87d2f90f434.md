### Title
`spawn_neuron` Minimum-Stake Pre-Check Uses Stale 5% Worst-Case Modulation While Mission 70 Allows −10% — (`rs/nns/governance/src/governance.rs`)

### Summary

`spawn_neuron` in NNS Governance guards against spawning a neuron whose eventual ICP stake would fall below `neuron_minimum_stake_e8s` by computing a "least possible stake" using a hardcoded 5% worst-case maturity modulation. The Mission 70 maturity-modulation algorithm, now live in production, has a lower bound of −10% (−1 000 permyriad). The pre-check therefore under-estimates the worst case by a factor of two, allowing neurons to be spawned whose stake will be below the minimum once the actual modulation is applied.

### Finding Description

In `spawn_neuron`, the pre-spawn minimum-stake check is:

```rust
// rs/nns/governance/src/governance.rs  line 2666
let least_possible_stake = (maturity_to_spawn as f64 * (1_f64 - 0.05)) as u64;

if least_possible_stake < economics.neuron_minimum_stake_e8s {
    return Err(GovernanceError::new_with_message(
        ErrorType::InsufficientFunds,
        "There isn't enough maturity to spawn a new neuron due to worst case maturity modulation.",
    ));
}
``` [1](#0-0) 

The hardcoded `0.05` (5%) was correct when the old CMC-based modulation range was `[−500, +500]` permyriad (i.e., ±5%). The Mission 70 algorithm, implemented in `update_icp_xdr_rate_related_data.rs`, defines a new range:

```rust
// rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs
pub(crate) const MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70: i64 = -1_000;  // −10 %
pub(crate) const MATURITY_MODULATION_MAX_PERMYRIAD_MISSION_70: i64 =    200;  // +2 %
``` [2](#0-1) 

`maybe_spawn_neurons` reads the new `heap_data.maturity_modulation.current_value_permyriad` field (populated by the Mission 70 task) and passes it directly to `apply_maturity_modulation`:

```rust
// rs/nns/governance/src/governance.rs  line 6427-6435
let maturity_modulation = match self
    .heap_data
    .maturity_modulation
    .as_ref()
    .and_then(|m| m.current_value_permyriad)
{ ... Some(value) => value };
``` [3](#0-2) 

`apply_maturity_modulation` with `−1 000` permyriad multiplies the maturity by `9 000 / 10 000 = 0.90`:

```rust
// rs/nervous_system/governance/src/maturity_modulation/mod.rs
pub const MIN_MATURITY_MODULATION_PERMYRIAD: i32 = -500;   // old range
// Mission 70 actual minimum: -1_000 permyriad = -10 %
``` [4](#0-3) 

**Concrete example** (with `neuron_minimum_stake_e8s = 1 ICP = 100_000_000 e8s`):

| maturity_to_spawn | least_possible_stake (×0.95) | passes check? | actual stake at −10% (×0.90) | below minimum? |
|---|---|---|---|---|
| 105_263_158 e8s | 100_000_000 | ✓ | 94_736_842 | ✓ — invariant violated |

Any maturity in the range `(neuron_minimum_stake_e8s / 0.90, neuron_minimum_stake_e8s / 0.95]` passes the pre-check but produces a below-minimum stake when the modulation is −10%.

### Impact Explanation

A neuron controller can deliberately choose a `percentage_to_spawn` that places `maturity_to_spawn` in the vulnerable range. The child neuron is created in spawning state; seven days later `maybe_spawn_neurons` mints ICP at the actual (−10%) modulation, producing a neuron whose `cached_neuron_stake_e8s` is below `neuron_minimum_stake_e8s`. This violates the core governance invariant that all neurons must hold at least the minimum stake, and can corrupt voting-power accounting for that neuron.

### Likelihood Explanation

The Mission 70 modulation is already live and its range is [−10%, +2%]. For the bug to manifest, the daily modulation only needs to be between −5% and −10% — a realistic scenario during sustained ICP price declines. The entry path is a standard unprivileged `manage_neuron` → `Spawn` call, requiring no special permissions beyond controlling a neuron with sufficient maturity.

### Recommendation

Replace the hardcoded `0.05` with the actual worst-case permyriad constant, using integer arithmetic consistent with `apply_maturity_modulation`:

```rust
// Use the Mission 70 lower bound: -1_000 permyriad = multiply by 9_000/10_000
let least_possible_stake = maturity_to_spawn
    .saturating_mul(9_000)   // 10_000 + MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70
    / 10_000;
```

This mirrors the exact arithmetic used in `apply_maturity_modulation` and avoids floating-point imprecision.

### Proof of Concept

1. Governance is running with Mission 70 maturity modulation at −10% (−1 000 permyriad).
2. Neuron A has `maturity_e8s_equivalent = 105_263_158` (just above `100_000_000 / 0.95`).
3. Controller calls `manage_neuron` → `Spawn { percentage_to_spawn: 100 }`.
4. `spawn_neuron` computes `least_possible_stake = (105_263_158 as f64 * 0.95) as u64 = 100_000_000` — passes the check.
5. Child neuron is created in spawning state with `maturity_e8s_equivalent = 105_263_158`.
6. After 7 days, `maybe_spawn_neurons` calls `apply_maturity_modulation(105_263_158, -1_000)` → `105_263_158 * 9_000 / 10_000 = 94_736_842`.
7. Child neuron's `cached_neuron_stake_e8s` is set to `94_736_842`, which is below `neuron_minimum_stake_e8s = 100_000_000`. [5](#0-4) [6](#0-5)

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

**File:** rs/nns/governance/src/governance.rs (L6427-6435)
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
