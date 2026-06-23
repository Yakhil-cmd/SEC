### Title
Integer Truncation in `Swap::scale()` Causes Systematic SNS Token Loss for Swap Participants - (File: rs/sns/swap/src/swap.rs)

### Summary
The `Swap::scale()` function in the SNS Swap canister performs integer division to compute each participant's SNS token allocation. Because integer division truncates the remainder, the sum of all individual allocations is strictly less than `sns_token_e8s` whenever the division is not exact. The "lost" tokens remain locked in the swap canister's SNS ledger account and are never distributed to any participant, analogous to the Gumball bug where excess tokens were taken but not returned.

### Finding Description
During swap finalization, `create_sns_neuron_recipes()` calls `Swap::scale()` for every direct participant and every Neurons' Fund neuron to compute how many SNS tokens each participant receives:

```rust
fn scale(amount_icp_e8s: u64, total_sns_e8s: u64, total_icp_e8s: NonZeroU64) -> u64 {
    let r = (amount_icp_e8s as u128)
        .saturating_mul(total_sns_e8s as u128)
        .div(NonZeroU128::from(total_icp_e8s));
    r as u64
}
```

The formula is `floor((participant_icp * total_sns) / total_icp)`. Integer division truncates the fractional part. The code itself tracks `total_sns_tokens_sold_e8s` and logs a warning when the sum is less than `sns_being_offered_e8s`, but the remainder is never redistributed — it stays in the swap canister's SNS ledger balance and is never claimed by anyone.

**Concrete example:**
- `sns_token_e8s = 10` (10 e8s of SNS tokens offered)
- Two participants: Alice contributes 3 ICP, Bob contributes 7 ICP → `total_icp = 10`
- Alice: `floor(3 * 10 / 10) = 3` ✓
- Bob: `floor(7 * 10 / 10) = 7` ✓ (exact, no loss here)

Now with three participants: Alice 1 ICP, Bob 1 ICP, Carol 1 ICP → `total_icp = 3`, `sns_token_e8s = 10`:
- Alice: `floor(1 * 10 / 3) = 3`
- Bob: `floor(1 * 10 / 3) = 3`
- Carol: `floor(1 * 10 / 3) = 3`
- Total distributed: 9. **1 SNS token e8s is permanently lost.**

The `generate_vesting_schedule` / `apportion_approximately_equally` functions correctly handle remainder distribution *within* a single participant's neuron basket, but the per-participant `scale()` call itself discards the cross-participant remainder.

### Impact Explanation
Every SNS swap finalization where `(participant_icp * total_sns)` is not exactly divisible by `total_icp` results in a non-zero remainder of SNS tokens that are allocated to no one. These tokens remain in the swap canister's SNS ledger subaccount and are inaccessible. The magnitude of loss is bounded by `(number_of_participants - 1)` e8s, which for a swap with many participants and a high-value SNS token can represent a meaningful amount. The tokens are permanently stranded — there is no sweep or cleanup mechanism for the remainder.

**Vulnerability class:** Ledger conservation bug — tokens enter the swap canister but a portion is never distributed.

### Likelihood Explanation
This occurs in every SNS swap finalization where the ICP contributions do not divide the SNS token pool exactly. Given that ICP contributions are arbitrary user-chosen amounts (subject only to min/max bounds), non-exact division is the common case, not the exception. Any unprivileged user who participates in an SNS swap triggers this path. The entry point is `refresh_buyer_token_e8s` (open to any ingress caller during the OPEN lifecycle), and the loss materializes automatically at finalization via `create_sns_neuron_recipes`.

### Recommendation
After computing all per-participant SNS allocations via `scale()`, compute the remainder as `sns_token_e8s - total_sns_tokens_sold_e8s` and distribute it to one or more participants (e.g., add it to the last participant's allocation, or use `apportion_approximately_equally` across all participants). Alternatively, refund the remainder of SNS tokens back to the SNS treasury/governance canister rather than leaving them stranded.

### Proof of Concept

**Root cause — `Swap::scale()` truncates:** [1](#0-0) 

**Called per-participant in `create_sns_neuron_recipes()`:** [2](#0-1) 

**Remainder tracked but never redistributed:** [3](#0-2) 

**`apportion_approximately_equally` (used only within a single basket, not across participants) correctly handles remainders — but is not applied to the cross-participant case:** [4](#0-3) 

**Neurons' Fund participants suffer the same truncation:** [5](#0-4)

### Citations

**File:** rs/sns/swap/src/swap.rs (L203-207)
```rust
pub fn apportion_approximately_equally(total: u64, len: u64) -> Result<Vec<u64>, String> {
    let quotient = total
        .checked_div(len)
        .ok_or_else(|| format!("Unable to divide total={total} by len={len}"))?;
    let remainder = total % len; // For unsigned integers, % cannot overflow.
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

**File:** rs/sns/swap/src/swap.rs (L832-834)
```rust
        // Keep track of SNS tokens sold just to check that the amount
        // is correct at the end.
        let mut total_sns_tokens_sold_e8s: u64 = 0;
```

**File:** rs/sns/swap/src/swap.rs (L848-852)
```rust
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
