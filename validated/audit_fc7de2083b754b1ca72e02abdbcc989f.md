### Title
Systematic Floor-Division Truncation in `Swap::scale` Causes Permanent SNS Token Stranding — (File: `rs/sns/swap/src/swap.rs`)

### Summary
The `Swap::scale` function uses integer floor division (truncation toward zero) to compute each participant's proportional SNS token allocation during swap finalization. When called for every direct participant and every Neurons' Fund neuron, the cumulative truncation error causes the sum of all individual allocations to be strictly less than `sns_being_offered_e8s`. The difference — the "change" — is logged but never redistributed, leaving SNS tokens permanently stranded in the swap canister's account.

### Finding Description
During SNS swap finalization, `create_sns_neuron_recipes` calls `Swap::scale` for each buyer to compute their proportional SNS token allocation:

```rust
fn scale(amount_icp_e8s: u64, total_sns_e8s: u64, total_icp_e8s: NonZeroU64) -> u64 {
    let r = (amount_icp_e8s as u128)
        .saturating_mul(total_sns_e8s as u128)
        .div(NonZeroU128::from(total_icp_e8s));
    r as u64
}
``` [1](#0-0) 

The `.div(NonZeroU128::from(total_icp_e8s))` is Rust integer division, which truncates toward zero. For each participant `i`:

```
allocation_i = floor(amount_icp_e8s_i × total_sns_e8s / total_icp_e8s)
```

The truncation error per participant is in `[0, 1)` e8s. With N participants, the total stranded amount is in `[0, N)` e8s. The code explicitly tracks this discrepancy and logs it as "change": [2](#0-1) 

The same `scale` function is applied to Neurons' Fund neurons: [3](#0-2) 

The "change" tokens remain in the swap canister's SNS token account after `sweep_sns` completes, with no mechanism to redistribute or burn them.

This is the direct IC analog of the DSMath "round half up" bias: instead of a systematic upward bias, there is a systematic **downward bias** — every participant receives strictly fewer SNS tokens than their exact proportional share, and the aggregate shortfall is permanently locked.

### Impact Explanation
- **Ledger conservation bug**: SNS tokens offered in the swap are not fully distributed. The stranded amount scales with participant count — up to `MAX_LIST_DIRECT_PARTICIPANTS_LIMIT` (20,000) direct participants plus Neurons' Fund neurons, meaning up to ~20,000+ e8s of SNS tokens can be permanently stranded per swap.
- Each participant receives a slightly smaller SNS neuron basket than their ICP contribution entitles them to.
- The stranded tokens are not burned, not redistributed, and not refundable — they are permanently locked in the swap canister's SNS ledger account.
- The bias is **systematic and deterministic**: it occurs in every swap with more than one participant where amounts do not divide evenly. [4](#0-3) 

### Likelihood Explanation
This triggers in **every** SNS swap with multiple participants where `amount_icp_e8s_i × total_sns_e8s` is not exactly divisible by `total_icp_e8s`. Given that ICP contributions are arbitrary u64 values in e8s and `total_sns_e8s` is also an arbitrary u64, exact divisibility is the exception, not the rule. Any unprivileged user who participates in an open SNS swap (via `refresh_buyer_tokens`) contributes to the truncation error.

### Recommendation
Replace the per-participant floor division with a remainder-aware distribution. The codebase already contains `apportion_approximately_equally` (used in `generate_vesting_schedule`) which correctly distributes a total across N pieces while preserving the exact sum via Euclidean remainder distribution: [5](#0-4) 

Alternatively, after computing all per-participant allocations via `scale`, assign the remaining "change" tokens to one participant (e.g., the largest contributor), or burn them via the SNS ledger's burn endpoint, rather than leaving them stranded.

### Proof of Concept
**Concrete example** (3 participants, 10 SNS tokens = 1,000,000,000 e8s offered, 3 ICP = 300,000,000 e8s total):

- Participant A: 100,000,000 ICP e8s → `scale(100_000_000, 1_000_000_000, 300_000_000)` = `floor(333_333_333.33...)` = **333,333,333 e8s**
- Participant B: 100,000,000 ICP e8s → **333,333,333 e8s**
- Participant C: 100,000,000 ICP e8s → **333,333,333 e8s**
- **Total distributed**: 999,999,999 e8s
- **Stranded**: 1 e8s (0.00000001 SNS tokens) — permanently locked in swap canister

With 20,000 participants each contributing non-divisible amounts, the stranded amount can reach up to ~20,000 e8s of SNS tokens per swap, permanently lost from circulation. [6](#0-5)

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

**File:** rs/sns/swap/src/swap.rs (L832-852)
```rust
        // Keep track of SNS tokens sold just to check that the amount
        // is correct at the end.
        let mut total_sns_tokens_sold_e8s: u64 = 0;

        // =====================================================================
        // ===            This is where the actual swap happens              ===
        // =====================================================================
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

**File:** rs/sns/swap/src/swap.rs (L920-924)
```rust
                    let amount_sns_e8s = Swap::scale(
                        neurons_fund_neuron.amount_icp_e8s,
                        sns_being_offered_e8s,
                        total_participant_icp_e8s,
                    );
```

**File:** rs/sns/swap/src/swap.rs (L976-987)
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
