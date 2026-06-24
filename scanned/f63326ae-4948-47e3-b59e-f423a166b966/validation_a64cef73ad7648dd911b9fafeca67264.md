### Title
Stale Worst-Case Maturity Modulation Constant in `spawn_neuron` Allows Below-Minimum-Stake Neuron Creation - (File: `rs/nns/governance/src/governance.rs`)

### Summary

`spawn_neuron` in NNS Governance uses a hardcoded 5% worst-case maturity modulation to gate whether a neuron has enough maturity to spawn. After Mission 70, the actual worst-case modulation applied by `maybe_spawn_neurons` is -10% (−1,000 permyriad). The pre-check and the execution path are now mismatched: a neuron controller can pass the pre-check with maturity that will produce a below-minimum-stake neuron when the modulation is between −5% and −10%.

### Finding Description

In `rs/nns/governance/src/governance.rs`, `spawn_neuron` computes the minimum possible spawned stake as:

```rust
let least_possible_stake = (maturity_to_spawn as f64 * (1_f64 - 0.05)) as u64;
``` [1](#0-0) 

This hardcodes 5% as the worst-case modulation. However, `maybe_spawn_neurons` now reads the Mission 70 maturity modulation from `heap_data.maturity_modulation`, whose lower bound is `MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 = -1_000` permyriad (−10%): [2](#0-1) 

The actual modulation applied at spawn time uses `apply_maturity_modulation` with this Mission 70 value: [3](#0-2) 

`apply_maturity_modulation` computes `amount * (10_000 + basis_points) / 10_000`, so at −1,000 permyriad the spawned stake is 90% of maturity: [4](#0-3) 

The CHANGELOG confirms the switch to Mission 70 modulation for spawning: [5](#0-4) 

The pre-check in `spawn_neuron` was never updated to reflect the new −10% floor, leaving a gap between what the check permits and what the execution path can produce.

### Impact Explanation

A neuron controller (unprivileged ingress sender) can call `manage_neuron` → `Spawn` with `maturity_to_spawn = M` where:

```
M * 0.95 >= neuron_minimum_stake_e8s   (passes the pre-check)
M * 0.90 <  neuron_minimum_stake_e8s   (below minimum after −10% modulation)
```

Concretely, if `neuron_minimum_stake_e8s = 1 ICP = 100_000_000 e8s`, any `M` in the range `[100_000_000 / 0.95, 100_000_000 / 0.90)` ≈ `[105_263_158, 111_111_111)` e8s passes the check but produces a spawned neuron with stake below 1 ICP. This violates the protocol invariant that all neurons hold at least `neuron_minimum_stake_e8s`, and the neuron is permanently created in that state since there is no post-spawn correction.

### Likelihood Explanation

The Mission 70 modulation moves at most 30 permyriad/day toward its target. Reaching −500 permyriad (the old floor) takes ~17 days from zero; reaching −1,000 permyriad takes ~33 days. Once the modulation is below −500 permyriad, any neuron controller whose maturity falls in the vulnerable window can trigger the bug. The window is approximately 5.5% of `neuron_minimum_stake_e8s` wide, which is a realistic maturity amount for active NNS participants. The entry path requires only a standard `manage_neuron` ingress call — no privileged access.

### Recommendation

Replace the hardcoded `0.05` with the actual Mission 70 worst-case modulation constant:

```rust
// Use the actual Mission 70 worst-case: -1_000 permyriad = -10%
let least_possible_stake = apply_maturity_modulation(
    maturity_to_spawn,
    MATURITY_MODULATION_MIN_PERMYRIAD_MISSION_70 as i32,
).unwrap_or(0);
```

This mirrors the approach already used in SNS `disburse_maturity`, which explicitly calls `apply_maturity_modulation` with `MIN_MATURITY_MODULATION_PERMYRIAD` for its pre-check: [6](#0-5) 

### Proof of Concept

1. Wait for (or observe) Mission 70 maturity modulation to reach below −500 permyriad (e.g., −600 permyriad after a sustained ICP price decline).
2. As a neuron controller with `maturity_e8s_equivalent = 106_000_000` (1.06 ICP):
   - Pre-check: `106_000_000 * 0.95 = 100_700_000 >= 100_000_000` → **passes**.
3. Call `manage_neuron` → `Spawn { percentage_to_spawn: 100, ... }`. The child neuron is created in spawning state with `maturity_e8s_equivalent = 106_000_000`.
4. When `maybe_spawn_neurons` fires with modulation = −600 permyriad:
   - `apply_maturity_modulation(106_000_000, -600)` = `106_000_000 * 9_400 / 10_000 = 99_640_000`.
5. The child neuron is minted with `cached_neuron_stake_e8s = 99_640_000 < 100_000_000 = neuron_minimum_stake_e8s`, violating the protocol minimum-stake invariant.

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

**File:** rs/nns/governance/src/governance.rs (L6427-6502)
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

        // Acquire the global "spawning" lock.
        self.heap_data.spawning_neurons = Some(true);

        // Filter all the neurons that are currently in "spawning" state.
        // Do this here to avoid having to borrow *self while we perform changes below.
        // Spawning neurons must have maturity, and no neurons in stable storage should have maturity.
        let ready_to_spawn_ids = self
            .neuron_store
            .list_ready_to_spawn_neuron_ids(now_seconds);

        // We can't alias ready_to_spawn_ids in the loop below, but the TLA model needs access to it,
        // so we clone it here.
        #[cfg(feature = "tla")]
        let mut _tla_ready_to_spawn_ids: BTreeSet<u64> =
            ready_to_spawn_ids.iter().map(|nid| nid.id).collect();

        for neuron_id in ready_to_spawn_ids {
            // Actually mint the neuron's ICP.
            let in_flight_command = NeuronInFlightCommand {
                timestamp: now_seconds,
                command: Some(InFlightCommand::Spawn(neuron_id)),
            };

            // Add the neuron to the set of neurons undergoing ledger updates.
            match self.lock_neuron_for_command(neuron_id.id, in_flight_command.clone()) {
                Ok(mut lock) => {
                    // Since we're multiplying a potentially pretty big number by up to 10500, do
                    // the calculations as u128 before converting back.
                    let neuron = self
                        .with_neuron(&neuron_id, |neuron| neuron.clone())
                        .expect("Neuron should exist, just found in list");

                    let original_maturity = neuron.maturity_e8s_equivalent;
                    let subaccount = neuron.subaccount();

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

**File:** rs/nns/governance/CHANGELOG.md (L20-22)
```markdown
* Neuron spawning and maturity disbursement finalization now read the locally
  computed Mission 70 maturity modulation (derived from the XRC-backed price
  history) instead of the CMC-polled `cached_daily_maturity_modulation_basis_points`.
```

**File:** rs/sns/governance/src/governance.rs (L1654-1678)
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

        if worst_case_maturity_modulation < transaction_fee_e8s {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "If worst case maturity modulation is applied (-5%) then this neuron would \
                     disburse {worst_case_maturity_modulation} e8s, but can't disburse an amount less than the transaction fee \
                     of {transaction_fee_e8s} e8s."
                ),
            ));
        }
```
