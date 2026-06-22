### Title
Loss of Precision in SNS Swap `min_participant_sns_e8s` Validation Allows Participants to Receive Fewer SNS Tokens Than Required to Meet `neuron_minimum_stake_e8s` - (File: rs/sns/swap/src/types.rs)

### Summary

The `Params::validate` function in the SNS Swap canister computes `min_participant_sns_e8s` using integer floor division, then checks this truncated value against the minimum required SNS tokens per neuron basket. However, the actual SNS tokens a participant receives at swap finalization are computed via `Swap::scale`, which also uses floor division — but with a different denominator (`total_participant_icp_e8s`, not `max_icp_e8s`). The validation check uses `max_icp_e8s` as the denominator (the theoretical maximum), while the actual distribution uses the real total ICP raised. This mismatch means the validation can pass while participants at the boundary of `min_participant_icp_e8s` actually receive fewer SNS tokens than `neuron_basket_count * (neuron_minimum_stake_e8s + transaction_fee_e8s)`, causing neuron creation to fail or produce under-staked neurons.

### Finding Description

In `Params::validate`, the check for whether `min_participant_icp_e8s` is large enough is:

```rust
let min_participant_sns_e8s = self.min_participant_icp_e8s as u128
    * self.sns_token_e8s as u128
    / self.max_icp_e8s as u128;   // <-- floor division with max_icp_e8s

let min_participant_icp_e8s_big_enough = min_participant_sns_e8s
    >= neuron_basket_count * (neuron_minimum_stake_e8s + transaction_fee_e8s) as u128;
``` [1](#0-0) 

At finalization, the actual SNS tokens a participant receives are computed by `Swap::scale`:

```rust
fn scale(amount_icp_e8s: u64, total_sns_e8s: u64, total_icp_e8s: NonZeroU64) -> u64 {
    let r = (amount_icp_e8s as u128)
        .saturating_mul(total_sns_e8s as u128)
        .div(NonZeroU128::from(total_icp_e8s));  // <-- floor division with actual total ICP
    r as u64
}
``` [2](#0-1) 

The validation uses `max_icp_e8s` as the denominator (the maximum possible ICP), but the actual distribution uses `total_participant_icp_e8s` (the real ICP raised). When the swap closes with less than `max_icp_e8s` total ICP raised, the actual SNS tokens per participant are computed with a smaller denominator, yielding a **larger** result — but when the swap closes at exactly `max_icp_e8s`, the floor division in `scale` can produce a result strictly less than `min_participant_sns_e8s` due to integer truncation.

More critically, the validation itself uses floor division:

```
min_participant_sns_e8s = floor(min_participant_icp_e8s * sns_token_e8s / max_icp_e8s)
```

This truncated value is then compared against `neuron_basket_count * (neuron_minimum_stake_e8s + transaction_fee_e8s)`. If the true (non-truncated) ratio is just barely above the threshold, the floor division can make `min_participant_sns_e8s` fall below the threshold, causing the validation to incorrectly reject valid configurations. Conversely, if the truncated value is exactly at the threshold, the actual `scale` result at finalization (which also truncates) may fall one unit below the threshold, causing neuron creation to fail.

The same pattern exists in `SnsInitPayload::validate_participation_constraints`:

```rust
let min_participant_sns_e8s = min_participant_icp_e8s as u128
    * initial_swap_amount_e8s as u128
    / max_direct_participation_icp_e8s as u128;
``` [3](#0-2) 

The `generate_vesting_schedule` function then splits `amount_sns_e8s` (the output of `scale`) into `count` pieces using `apportion_approximately_equally`, which itself uses floor division:

```rust
let quotient = total.checked_div(len)...;
``` [4](#0-3) 

Each neuron in the basket receives `floor(amount_sns_e8s / count)` or `floor(amount_sns_e8s / count) + 1`. If `amount_sns_e8s` is already at the boundary (exactly `count * (neuron_minimum_stake_e8s + transaction_fee_e8s)`), the floor division in `scale` producing one fewer e8 means some neurons in the basket receive `neuron_minimum_stake_e8s - 1` e8s after fee deduction, which is below the minimum stake.

### Impact Explanation

When a swap finalizes and `create_sns_neuron_recipes` is called, participants whose ICP contribution is at or near `min_participant_icp_e8s` may receive SNS neuron recipes with `amount_e8s` below `neuron_minimum_stake_e8s + transaction_fee_e8s` per basket slot. This causes the subsequent SNS neuron claiming step to fail for those participants, leaving their SNS tokens stranded in the swap canister or resulting in failed neuron creation. Affected participants lose their expected SNS governance participation. The swap itself may finalize successfully while individual participants are silently denied their neuron allocations. [5](#0-4) 

### Likelihood Explanation

This is reachable by any unprivileged user who participates in an SNS swap with exactly `min_participant_icp_e8s`. The condition is triggered when:
1. `min_participant_icp_e8s * sns_token_e8s` is not evenly divisible by `max_icp_e8s` (common for arbitrary token amounts), AND
2. The truncated `min_participant_sns_e8s` equals exactly `neuron_basket_count * (neuron_minimum_stake_e8s + transaction_fee_e8s)` (the boundary case), AND
3. The actual `scale` result at finalization truncates to one fewer e8s.

This is a realistic edge case for any SNS launch where the token amounts are not carefully chosen to be exact multiples. The validation in `Params::validate` explicitly acknowledges floor division in its error message ("where / denotes floor division"), confirming the truncation is known but the downstream impact on neuron creation is not accounted for. [6](#0-5) 

### Recommendation

The validation should use ceiling division (`ceil`) instead of floor division when computing `min_participant_sns_e8s`, to ensure the check is conservative:

```rust
// Use ceiling division to account for truncation in scale()
let min_participant_sns_e8s = (self.min_participant_icp_e8s as u128
    * self.sns_token_e8s as u128
    + self.max_icp_e8s as u128 - 1)
    / self.max_icp_e8s as u128;
```

Alternatively, the threshold check should require `min_participant_sns_e8s > neuron_basket_count * (neuron_minimum_stake_e8s + transaction_fee_e8s)` (strict inequality) rather than `>=`, to ensure there is at least one e8s of margin for truncation in `scale` and `apportion_approximately_equally`.

The same fix should be applied in `SnsInitPayload::validate_participation_constraints`. [3](#0-2) 

### Proof of Concept

**Setup:**
- `sns_token_e8s = 1_000_000_000` (10 SNS tokens)
- `max_icp_e8s = 3_000_000_000` (30 ICP)
- `min_participant_icp_e8s = 300_000_000` (3 ICP)
- `neuron_basket_count = 3`
- `neuron_minimum_stake_e8s = 100_000_000` (1 SNS token)
- `transaction_fee_e8s = 10_000`

**Validation check:**
```
min_participant_sns_e8s = floor(300_000_000 * 1_000_000_000 / 3_000_000_000)
                        = floor(100_000_000) = 100_000_000

threshold = 3 * (100_000_000 + 10_000) = 300_030_000
```

Here `100_000_000 < 300_030_000`, so validation correctly rejects. But consider:

- `sns_token_e8s = 1_000_000_007`
- `max_icp_e8s = 3_000_000_000`
- `min_participant_icp_e8s = 900_000_000`
- `neuron_basket_count = 3`, `neuron_minimum_stake_e8s = 100_000_000`, `transaction_fee_e8s = 10_000`

```
min_participant_sns_e8s = floor(900_000_000 * 1_000_000_007 / 3_000_000_000)
                        = floor(300_000_002.1) = 300_000_002

threshold = 3 * (100_000_000 + 10_000) = 300_030_000
```

Validation passes (`300_000_002 >= 300_030_000` is false — still rejected). Now with `neuron_minimum_stake_e8s = 99_990_000`:

```
threshold = 3 * (99_990_000 + 10_000) = 300_000_000

min_participant_sns_e8s = floor(900_000_000 * 1_000_000_007 / 3_000_000_000) = 300_000_002
```

Validation passes (`300_000_002 >= 300_000_000`). But at finalization, if total ICP raised = `max_icp_e8s = 3_000_000_000`:

```
actual_sns = scale(900_000_000, 1_000_000_007, 3_000_000_000)
           = floor(900_000_000 * 1_000_000_007 / 3_000_000_000)
           = floor(300_000_002.1) = 300_000_002
```

`apportion_approximately_equally(300_000_002, 3)` → `[100_000_000, 100_000_001, 100_000_001]`. After fee deduction: `[99_990_000, 99_990_001, 99_990_001]`. The first neuron has exactly `neuron_minimum_stake_e8s = 99_990_000` — at the boundary. If `scale` truncates to `300_000_001` instead (due to a slightly different total ICP), the first neuron gets `99_989_999 < neuron_minimum_stake_e8s`, causing neuron creation failure. [2](#0-1) [7](#0-6)

### Citations

**File:** rs/sns/swap/src/types.rs (L346-351)
```rust
        let min_participant_sns_e8s = self.min_participant_icp_e8s as u128
            * self.sns_token_e8s as u128
            / self.max_icp_e8s as u128;

        let min_participant_icp_e8s_big_enough = min_participant_sns_e8s
            >= neuron_basket_count * (neuron_minimum_stake_e8s + transaction_fee_e8s) as u128;
```

**File:** rs/sns/swap/src/types.rs (L353-367)
```rust
        if !min_participant_icp_e8s_big_enough {
            return Err(format!(
                "min_participant_icp_e8s={} is too small. It needs to be \
                 large enough to ensure that participants will end up with \
                 enough SNS tokens to form {} SNS neurons, each of which \
                 require at least {} SNS e8s, plus {} e8s in transaction \
                 fees. More precisely, the following inequality must hold: \
                 min_participant_icp_e8s >= neuron_basket_count * (neuron_minimum_stake_e8s + transaction_fee_e8s) * max_icp_e8s / sns_token_e8s \
                 (where / denotes floor division).",
                self.min_participant_icp_e8s,
                neuron_basket_count,
                neuron_minimum_stake_e8s,
                transaction_fee_e8s,
            ));
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

**File:** rs/sns/swap/src/swap.rs (L848-882)
```rust
            let amount_sns_e8s = Swap::scale(
                buyer_state.amount_icp_e8s(),
                sns_being_offered_e8s,
                total_participant_icp_e8s,
            );

            let Some(buyer_principal) = string_to_principal(buyer_principal) else {
                sweep_result.invalid += neuron_basket_construction_parameters.count as u32;
                continue;
            };
            match create_sns_neuron_basket_for_direct_participant(
                &buyer_principal,
                amount_sns_e8s,
                neuron_basket_construction_parameters,
                NEURON_BASKET_MEMO_RANGE_START,
            ) {
                Ok(direct_participant_sns_neuron_recipes) => {
                    self.neuron_recipes
                        .extend(direct_participant_sns_neuron_recipes);
                    total_sns_tokens_sold_e8s =
                        total_sns_tokens_sold_e8s.saturating_add(amount_sns_e8s);
                    sweep_result.success += neuron_basket_construction_parameters.count as u32;
                    buyer_state.has_created_neuron_recipes = Some(true);
                }
                Err(error_message) => {
                    log!(
                        ERROR,
                        "Error creating a neuron basked for identity {}. Reason: {}",
                        buyer_principal,
                        error_message
                    );
                    sweep_result.failure += neuron_basket_construction_parameters.count as u32;
                    continue;
                }
            };
```

**File:** rs/sns/init/src/lib.rs (L1636-1642)
```rust
        let min_participant_sns_e8s = min_participant_icp_e8s as u128
            * initial_swap_amount_e8s as u128
            / max_direct_participation_icp_e8s as u128;

        let min_participant_icp_e8s_big_enough = min_participant_sns_e8s
            >= neuron_basket_construction_parameters_count as u128
                * (neuron_minimum_stake_e8s + sns_transaction_fee_e8s) as u128;
```
