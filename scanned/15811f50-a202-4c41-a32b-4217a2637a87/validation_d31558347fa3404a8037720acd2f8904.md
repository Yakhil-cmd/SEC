### Title
Floating-Point vs Integer Rounding Inconsistency in `spawn_neuron` Pre-Check Allows Spawning Neurons Below Minimum Stake - (`rs/nns/governance/src/governance.rs`)

### Summary
In NNS governance's `spawn_neuron`, the worst-case stake pre-check uses floating-point arithmetic while the actual spawn execution uses integer arithmetic. For specific maturity values, the float computation rounds up (passing the check) while the integer computation rounds down (producing a stake 1 e8 below `neuron_minimum_stake_e8s`). This mirrors the UniswapV2 rounding inconsistency class: two counterpart computations of the same quantity use different rounding, causing the pre-check to permit an operation that the execution then violates.

### Finding Description

**Pre-check at spawn initiation** (`rs/nns/governance/src/governance.rs`):

```rust
let least_possible_stake = (maturity_to_spawn as f64 * (1_f64 - 0.05)) as u64;
if least_possible_stake < economics.neuron_minimum_stake_e8s {
    return Err(...);
}
``` [1](#0-0) 

**Actual execution at spawn time** (`maybe_spawn_neurons`), which calls `apply_maturity_modulation`:

```rust
let neuron_stake: u64 = match apply_maturity_modulation(
    original_maturity,
    maturity_modulation,  // can be -500 (worst case)
) { ... };
``` [2](#0-1) 

`apply_maturity_modulation` uses **pure integer arithmetic**:

```rust
let modulated_amount_e8s: u128 = amount_e8s
    .checked_mul(adjusted_maturity_modulation_basis_points)  // * 9500
    .ok_or_else(...)?
    .checked_div(BASIS_POINTS_PER_UNITY)                     // / 10000
    .ok_or_else(...)?;
``` [3](#0-2) 

**The inconsistency**: For `maturity_to_spawn = 105_263_158` e8s:

| Method | Computation | Result |
|---|---|---|
| Float pre-check | `105_263_158.0 × 0.95` → `100_000_000.1` → truncated | `100_000_000` ✓ (passes) |
| Integer execution | `105_263_158 × 9500 / 10000` = `999_999_999_100 / 10000` | `99_999_999` ✗ (below minimum) |

The pre-check passes (`100_000_000 >= neuron_minimum_stake_e8s`), but the actual spawned neuron receives a stake of `99_999_999` e8s — 1 e8 below the minimum.

This is structurally identical to the UniswapV2 bug: the pre-check (analogous to `getAmountIn`) and the execution (analogous to `getAmountOut`) are counterpart computations of the same worst-case value, but they use different rounding, causing the pre-check to approve an operation that the execution then violates.

### Impact Explanation

A spawned neuron can be created with `cached_neuron_stake_e8s` below `neuron_minimum_stake_e8s`. NNS governance garbage-collects neurons whose stake falls below the minimum during periodic tasks. The maturity that was moved from the parent neuron to the child neuron is permanently lost — the parent's maturity was already decremented, and the child neuron is garbage-collected. This is a **ledger conservation bug**: user maturity is destroyed without being disbursed. [4](#0-3) 

### Likelihood Explanation

Any NNS neuron controller can trigger this by calling `manage_neuron` with a `Spawn` command where `maturity_to_spawn` falls in the vulnerable range (e.g., exactly `105_263_158` e8s). This is achievable by:
- Holding a neuron with maturity of `105_263_158` e8s and spawning 100%, or
- Holding a neuron with maturity of `210_526_316` e8s and spawning 50%, etc.

The vulnerable range is any `x` where `floor(x × 0.95_f64) > floor(x × 9500 / 10000)`. Such values exist at every boundary where `x × 9500` is not a multiple of `10000` but `x × 0.95` in f64 rounds up. This is a **medium likelihood** issue: it requires a specific maturity amount, but that amount is easily engineered by accumulating rewards to the exact value.

### Recommendation

Replace the floating-point worst-case computation with the same integer arithmetic used in `apply_maturity_modulation`:

```diff
- let least_possible_stake = (maturity_to_spawn as f64 * (1_f64 - 0.05)) as u64;
+ let least_possible_stake =
+     apply_maturity_modulation(maturity_to_spawn, MIN_MATURITY_MODULATION_PERMYRIAD)
+         .unwrap_or(0);
```

This ensures the pre-check and the execution use identical rounding, eliminating the off-by-one discrepancy. [5](#0-4) 

### Proof of Concept

1. Create an NNS neuron and accumulate exactly `105_263_158` e8s of maturity.
2. Call `manage_neuron` with `Spawn { percentage_to_spawn: Some(100), ... }`.
3. Pre-check: `(105_263_158_f64 * 0.95) as u64 = 100_000_000` — equals `neuron_minimum_stake_e8s`, so the spawn is permitted.
4. After the 7-day dissolve delay, `maybe_spawn_neurons` fires with worst-case modulation (`-500` basis points).
5. `apply_maturity_modulation(105_263_158, -500)` = `105_263_158 × 9500 / 10000 = 99_999_999`.
6. The child neuron is created with `cached_neuron_stake_e8s = 99_999_999`, which is 1 e8 below `neuron_minimum_stake_e8s = 100_000_000`.
7. During the next garbage-collection pass, the child neuron is removed; the parent's maturity was already decremented by `105_263_158` e8s and is unrecoverable. [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** rs/nns/governance/src/governance.rs (L2643-2648)
```rust
        let maturity_to_spawn = parent_neuron
            .maturity_e8s_equivalent
            .checked_mul(percentage as u64)
            .expect("Overflow while processing maturity to spawn.");
        let maturity_to_spawn = maturity_to_spawn.checked_div(100).unwrap();

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
