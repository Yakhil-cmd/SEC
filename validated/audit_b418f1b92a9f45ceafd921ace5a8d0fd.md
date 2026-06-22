### Title
Floor Division Rounding in `Swap::scale` Causes Systematic SNS Token Underdistribution to Swap Participants - (File: `rs/sns/swap/src/swap.rs`)

### Summary
The `Swap::scale` function uses integer floor division to compute each participant's SNS token allocation during swap finalization. Because each participant's share is independently truncated, the sum of all allocations is strictly less than the total SNS tokens offered, and the remainder is silently retained in the swap canister without redistribution. This is a direct analog to the Proteus rounding-error class: a systematic per-participant token loss caused by undocumented floor-rounding direction in a proportional-split calculation.

### Finding Description
In `create_sns_neuron_recipes`, for every direct buyer and every Neurons' Fund neuron, the canister calls:

```rust
let amount_sns_e8s = Swap::scale(
    buyer_state.amount_icp_e8s(),
    sns_being_offered_e8s,
    total_participant_icp_e8s,
);
```

`Swap::scale` is defined as:

```rust
fn scale(amount_icp_e8s: u64, total_sns_e8s: u64, total_icp_e8s: NonZeroU64) -> u64 {
    let r = (amount_icp_e8s as u128)
        .saturating_mul(total_sns_e8s as u128)
        .div(NonZeroU128::from(total_icp_e8s));   // integer floor division
    r as u64
}
``` [1](#0-0) 

The `.div(NonZeroU128::from(total_icp_e8s))` call is Rust's integer division, which truncates toward zero (floor). For each participant `i`, the true proportional share is `amount_i * total_sns / total_icp`, but the canister assigns `floor(amount_i * total_sns / total_icp)`. The fractional remainder (up to `(total_icp - 1) / total_icp < 1 e8`) is silently discarded per participant.

The code itself acknowledges the discrepancy in a log line but takes no corrective action:

```rust
log!(INFO,
    "... Participants receive a total of {} out of {} (change {});",
    total_sns_tokens_sold_e8s,
    sns_being_offered_e8s,
    sns_being_offered_e8s - total_sns_tokens_sold_e8s   // leftover, never redistributed
);
``` [2](#0-1) 

The leftover tokens remain in the swap canister's SNS ledger balance. There is no subsequent step that redistributes them to participants.

By contrast, the intra-basket split (`apportion_approximately_equally`) correctly preserves the total by distributing the remainder 1-by-1 to the last elements: [3](#0-2) 

The same discipline is absent at the cross-participant level in `Swap::scale`.

### Impact Explanation
Each participant loses up to 1 e8 (10⁻⁸ SNS tokens) per swap finalization due to floor truncation. With N participants the aggregate underdistribution is up to N e8s. The undistributed tokens are locked in the swap canister and inaccessible to participants. This breaks the invariant that `sum(participant_sns_allocations) == sns_token_e8s` and constitutes a ledger conservation bug: SNS tokens that were offered and paid for are never delivered.

**Concrete example**: 3 participants each contribute 1 ICP (1×10⁸ e8s); the swap offers 10 SNS tokens (10×10⁸ e8s). Each participant's allocation = `floor(1×10⁸ × 10×10⁸ / 3×10⁸)` = `floor(3.333…×10⁸)` = `3×10⁸`. Total distributed = `9×10⁸`. Leftover = `1×10⁸` SNS e8s — permanently stranded in the swap canister.

### Likelihood Explanation
This condition is triggered in every SNS decentralization swap that has more than one participant and where `total_icp_e8s` does not evenly divide `participant_icp_e8s × total_sns_e8s`. That is the common case for any real-world swap. Any unprivileged principal can participate in a swap via `refresh_buyer_tokens`, making the entry path fully reachable without any privileged access. [4](#0-3) 

### Recommendation
Replace the independent per-participant `Swap::scale` calls with a two-pass approach: compute floor allocations for all participants, sum them, then distribute the remainder (at most N e8s) to participants in round-robin or largest-remainder order — exactly as `apportion_approximately_equally` does within a single basket. Alternatively, after the loop, assign the residual `sns_being_offered_e8s - total_sns_tokens_sold_e8s` to the participant with the largest fractional loss. Document the chosen rounding direction explicitly.

### Proof of Concept
1. Launch an SNS swap with `sns_token_e8s = 10` (10 e8s) and `min_participants = 3`.
2. Three principals each contribute `1 ICP` (1 e8s), so `total_participant_icp_e8s = 3`.
3. At finalization, `Swap::scale(1, 10, 3)` = `floor(10/3)` = `3` for each participant.
4. `total_sns_tokens_sold_e8s` = `3 + 3 + 3` = `9`; `change` = `1` e8 stranded in the swap canister.
5. Each participant paid for `3.33…` e8s of SNS tokens but received only `3` — a permanent loss of `0.33…` e8s per participant, with no recourse. [5](#0-4) [6](#0-5)

### Citations

**File:** rs/sns/swap/src/swap.rs (L157-188)
```rust
impl NeuronBasketConstructionParameters {
    /// Chops `total_amount_e8s` into `self.count` pieces. Each gets doled out
    /// every `self.dissolve_delay_seconds`, starting from 0.
    ///
    /// # Arguments
    /// * `total_amount_e8s` - The total amount of tokens (in e8s) to be chopped up.
    fn generate_vesting_schedule(
        &self,
        total_amount_e8s: u64,
    ) -> Result<Vec<ScheduledVestingEvent>, String> {
        if self.count == 0 {
            return Err(
                "NeuronBasketConstructionParameters.count must be greater than zero".to_string(),
            );
        }

        let dissolve_delay_seconds_list = (0..(self.count))
            .map(|i| i * self.dissolve_delay_interval_seconds)
            .collect::<Vec<u64>>();

        let chunks_e8s = apportion_approximately_equally(total_amount_e8s, self.count)?;
        Ok(dissolve_delay_seconds_list
            .into_iter()
            .zip(chunks_e8s)
            .map(
                |(dissolve_delay_seconds, amount_e8s)| ScheduledVestingEvent {
                    dissolve_delay_seconds,
                    amount_e8s,
                },
            )
            .collect())
    }
```

**File:** rs/sns/swap/src/swap.rs (L203-244)
```rust
pub fn apportion_approximately_equally(total: u64, len: u64) -> Result<Vec<u64>, String> {
    let quotient = total
        .checked_div(len)
        .ok_or_else(|| format!("Unable to divide total={total} by len={len}"))?;
    let remainder = total % len; // For unsigned integers, % cannot overflow.

    // So far, we have only apportioned quotient * len. To reach the desired
    // total, we must still somehow add remainder (per Euclid's Division
    // Theorem). That is accomplished right after this.
    let mut result = vec![quotient; len as usize];

    // Divvy out the remainder: Starting from the last element, increment
    // elements by 1. The number of such increments performed here is remainder,
    // bringing our total back to the desired amount.
    if remainder >= result.len() as u64 {
        return Err(format!("Could not apportion {total} into {len} pieces"));
    }
    let mut iter_mut = result.iter_mut();
    for _ in 0..remainder {
        let element: &mut u64 = iter_mut
            .next_back()
            // We can prove that this will not panic:
            // The number of iterations of this loop is total % len.
            // This must be < len (by Euclid's Division Theorem).
            // Thus, the number of iterations that this loop goes through is < len.
            // Thus, the number of times next_back is called is < len.
            // next_back only returns None after len calls.
            // Therefore, next_back does not return None here.
            // Therefore, this expect will never panic.
            .ok_or_else(
                || format!("Ran out of elements to increment. total={total}, len={len}",),
            )?;

        // This cannot overflow because the result must be <= total. Thus, this
        // will not panic.
        *element = element.checked_add(1).ok_or_else(|| {
            format!("Incrementing element by 1 resulted in overflow. total={total}, len={len}",)
        })?;
    }

    Ok(result)
}
```

**File:** rs/sns/swap/src/swap.rs (L742-751)
```rust
    fn scale(amount_icp_e8s: u64, total_sns_e8s: u64, total_icp_e8s: NonZeroU64) -> u64 {
        assert!(amount_icp_e8s <= u64::from(total_icp_e8s));
        // Note that the multiplication cannot overflow as both factors fit in 64 bits.
        let r = (amount_icp_e8s as u128)
            .saturating_mul(total_sns_e8s as u128)
            .div(NonZeroU128::from(total_icp_e8s));
        // This follows logically from the initial assert `amount_icp_e8s <= total_icp_e8s`.
        assert!(r <= u64::MAX as u128);
        r as u64
    }
```

**File:** rs/sns/swap/src/swap.rs (L839-852)
```rust
        for (buyer_principal, buyer_state) in self.buyers.iter_mut() {
            // The case that on a previous attempt at creating this neuron recipe, it was
            // successfully created and recorded. Count the number of neuron recipes that
            // would have been created.
            if buyer_state.has_created_neuron_recipes == Some(true) {
                sweep_result.skipped += neuron_basket_construction_parameters.count as u32;
                continue;
            }

            let amount_sns_e8s = Swap::scale(
                buyer_state.amount_icp_e8s(),
                sns_being_offered_e8s,
                total_participant_icp_e8s,
            );
```

**File:** rs/sns/swap/src/swap.rs (L976-986)
```rust
        log!(
            INFO,
            "SNS Neuron Recipes Created; {} successes, {} failures, {} invalids, and {} skips. Participants receive a total of {} out of {} (change {});",
            sweep_result.success,
            sweep_result.failure,
            sweep_result.invalid,
            sweep_result.skipped,
            total_sns_tokens_sold_e8s,
            sns_being_offered_e8s,
            sns_being_offered_e8s - total_sns_tokens_sold_e8s
        );
```
