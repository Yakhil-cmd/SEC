### Title
Precision Loss via Integer Division in `compute_average_icp_xdr_rate_at_time` and `compute_maturity_modulation` Causes Neuron Owners to Receive Less ICP When Spawning — (`rs/nns/cmc/src/main.rs`)

### Summary

The Cycles Minting Canister (CMC) computes the ICP/XDR average rate and the maturity modulation using sequential integer divisions with no intermediate precision preservation. The truncated maturity modulation is then applied to neuron maturity at spawn time, causing neuron owners to receive systematically less ICP than the mathematically correct amount whenever the modulation is positive.

### Finding Description

**Step 1 — `compute_average_icp_xdr_rate_at_time` truncates the average rate.**

The function accumulates `xdr_permyriad_per_icp` values into a `u64` sum and divides by `size` using plain integer division:

```rust
xdr_permyriad_per_icp: sum / size,   // integer division, fractional part discarded
``` [1](#0-0) 

For a 30-day window (`NUM_DAYS_FOR_ICP_XDR_AVERAGE`), the truncation error is up to 29 units of `xdr_permyriad_per_icp` (each unit = 1/10 000 XDR per ICP). This truncated value is stored as the canonical average rate and fed into every downstream calculation.

**Step 2 — `compute_capped_maturity_modulation` divides again with integer arithmetic.**

The relative price change is computed as:

```rust
let difference_permyriad = difference.saturating_mul(10_000);
match difference_permyriad.checked_div(start_rate_value) { ... }
``` [2](#0-1) 

Because `start_rate_value` is already truncated from Step 1, the quotient carries a compounded error.

**Step 3 — `compute_maturity_modulation` divides the four-term sum by 4 with integer arithmetic.**

```rust
(rate1 + rate2 + rate3 + rate4) / 4
``` [3](#0-2) 

Each `rate_i` is an `i32` already capped to `[-500, 500]`. The final `/4` truncates up to 3 additional permyriad from the true average, compounding the earlier errors.

**Step 4 — The truncated modulation is applied to neuron maturity at spawn time.**

`apply_maturity_modulation` computes:

```rust
amount_e8s * (BASIS_POINTS_PER_UNITY + maturity_modulation_basis_points) / BASIS_POINTS_PER_UNITY
``` [4](#0-3) 

When `maturity_modulation_basis_points` is positive (ICP price rising), a truncated modulation directly reduces the ICP minted for the neuron owner.

The NNS governance canister reads this modulation value and applies it during `maybe_spawn_neurons`: [5](#0-4) 

### Impact Explanation

Every neuron owner who calls `spawn_neuron` or whose neuron is processed by the `maybe_spawn_neurons` timer while the maturity modulation is positive receives less ICP than the mathematically correct amount. The shortfall is:

```
shortfall_e8s ≈ maturity_e8s × truncation_permyriad / 10_000
```

With a truncation of up to 3 permyriad and a neuron maturity of 1 000 000 ICP (10^14 e8s), the shortfall reaches **~300 ICP per spawn event**. The error is systematic and accumulates across every spawn while the modulation is positive — it is never corrected or refunded.

### Likelihood Explanation

The maturity modulation is recomputed on every heartbeat that updates the ICP/XDR rate. Integer truncation occurs unconditionally on every computation. The NNS currently holds billions of ICP in neuron maturity, and spawning is a routine operation. The positive-modulation regime (rising ICP price) is common. The vulnerability therefore fires continuously in production.

### Recommendation

Replace the integer-division averaging chain with `Decimal` (already used elsewhere in the codebase, e.g., `rs/nns/governance/src/reward/calculation.rs`) or with a `u128`-scaled fixed-point representation:

1. In `compute_average_icp_xdr_rate_at_time`, accumulate into `u128` and return a `Decimal` or a scaled integer rather than truncating before returning.
2. In `compute_maturity_modulation`, sum the four `i32` terms and round to nearest rather than truncating: `(rate1 + rate2 + rate3 + rate4 + 2) / 4`.
3. Propagate the higher-precision value through `apply_maturity_modulation` before the final conversion to `u64`.

### Proof of Concept

Concrete numeric example with `NUM_DAYS_FOR_ICP_XDR_AVERAGE = 30`:

| Variable | True value | Computed (truncated) | Error |
|---|---|---|---|
| `xdr_permyriad_per_icp` average | 100 029.97 | 100 029 | −0.97 units |
| `compute_capped_maturity_modulation` (one term) | 501.3 | 501 | −0.3 permyriad |
| `compute_maturity_modulation` (four terms avg) | 125.75 | 125 | −0.75 permyriad |
| ICP minted for 1 000 000 ICP maturity | 1 012 575 ICP | 1 012 500 ICP | **−75 ICP** |

The shortfall is non-refundable: the governance canister zeros the neuron's maturity before the ledger mint, so the truncated amount is the only amount ever minted. [6](#0-5) [1](#0-0) [3](#0-2)

### Citations

**File:** rs/nns/cmc/src/main.rs (L967-972)
```rust
    if size > 0 {
        let sum: u64 = filtered_rates.into_iter().sum();
        Some(IcpXdrConversionRate {
            timestamp_seconds: day * 86_400,   // Start of the current day.
            xdr_permyriad_per_icp: sum / size, // The average of the valid data points.
        })
```

**File:** rs/nns/cmc/src/main.rs (L1052-1060)
```rust
fn compute_maturity_modulation(rates: &[IcpXdrConversionRate], time_s: u64) -> i32 {
    let day = time_s / 86_400;
    // Get the rate for four seven-day periods.
    let rate1 = compute_capped_maturity_modulation(rates, day - 7, day);
    let rate2 = compute_capped_maturity_modulation(rates, day - 14, day - 7);
    let rate3 = compute_capped_maturity_modulation(rates, day - 21, day - 14);
    let rate4 = compute_capped_maturity_modulation(rates, day - 28, day - 21);
    // Return the average as the final maturity modulation.
    (rate1 + rate2 + rate3 + rate4) / 4
```

**File:** rs/nns/cmc/src/main.rs (L1086-1094)
```rust
            let difference = end_rate_value.saturating_sub(start_rate_value);
            let difference_permyriad = difference.saturating_mul(10_000);
            match difference_permyriad.checked_div(start_rate_value) {
                Some(relative_change_permyriad) => relative_change_permyriad.clamp(
                    MIN_MATURITY_MODULATION_PERMYRIAD,
                    MAX_MATURITY_MODULATION_PERMYRIAD,
                ),
                None => 0,
            }
```

**File:** rs/nervous_system/governance/src/maturity_modulation/mod.rs (L22-26)
```rust
    let modulated_amount_e8s: u128 = amount_e8s
        .checked_mul(adjusted_maturity_modulation_basis_points)
        .ok_or_else(|| "Underflow or overflow when calculating maturity modulation".to_string())?
        .checked_div(BASIS_POINTS_PER_UNITY)
        .ok_or_else(|| "Underflow or overflow when calculating maturity modulation".to_string())?;
```

**File:** rs/nns/governance/src/governance.rs (L6484-6488)
```rust
                    let neuron_stake: u64 = match apply_maturity_modulation(
                        original_maturity,
                        maturity_modulation,
                    ) {
                        Ok(neuron_stake) => neuron_stake,
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
