### Title
Zero-SNS-Token Allocation via Integer Truncation in `Swap::scale` Silently Drops Participant Funds - (File: `rs/sns/swap/src/swap.rs`)

### Summary
The SNS Swap canister's `Swap::scale` function computes each participant's SNS token allocation using integer floor division. When a participant's ICP contribution is small relative to the total ICP raised, the result truncates to zero. A buyer whose `amount_sns_e8s` rounds to zero still has their ICP swept to SNS Governance but receives no SNS neurons — their funds are permanently lost with no error or refund path.

### Finding Description

During swap finalization, `create_sns_neuron_recipes` calls `Swap::scale` for every buyer to compute their proportional SNS token allocation:

```rust
fn scale(amount_icp_e8s: u64, total_sns_e8s: u64, total_icp_e8s: NonZeroU64) -> u64 {
    assert!(amount_icp_e8s <= u64::from(total_icp_e8s));
    let r = (amount_icp_e8s as u128)
        .saturating_mul(total_sns_e8s as u128)
        .div(NonZeroU128::from(total_icp_e8s));
    assert!(r <= u64::MAX as u128);
    r as u64
}
```

This is pure integer floor division: `(amount_icp_e8s * total_sns_e8s) / total_icp_e8s`. When `amount_icp_e8s * total_sns_e8s < total_icp_e8s`, the result is **zero**.

The result `amount_sns_e8s = 0` is then passed directly to `create_sns_neuron_basket_for_direct_participant`, which calls `generate_vesting_schedule(0)`, which calls `apportion_approximately_equally(0, count)`. This succeeds and returns a vector of `count` zeros — so `count` neuron recipes are created, each with `amount_e8s: 0`. No error is returned, no refund is triggered.

The ICP sweep (`sweep_icp`) then transfers the buyer's ICP to SNS Governance regardless. The buyer loses their ICP and receives neurons with zero stake, which are below `neuron_minimum_stake_e8s` and will fail to be claimed.

The analog to the original report's vulnerability class is: **a participant who contributes a very small amount of ICP relative to the total pool receives zero SNS tokens due to integer truncation, while their ICP is still consumed** — a direct ledger conservation bug where value enters the system but no corresponding token is minted.

The root cause is that `Swap::scale` can return 0 with no guard, and neither `create_sns_neuron_recipes` nor `create_sns_neuron_basket_for_direct_participant` checks whether `amount_sns_e8s == 0` before proceeding. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

### Impact Explanation

A buyer whose ICP contribution satisfies `amount_icp_e8s * sns_token_e8s < total_icp_e8s` (i.e., their proportional share rounds to zero) will:

1. Have their ICP swept to SNS Governance (permanently transferred, no refund).
2. Receive neuron recipes with `amount_e8s = 0` — neurons that cannot be claimed because they are below `neuron_minimum_stake_e8s`.
3. Lose their ICP with no recourse.

This is a **ledger conservation bug**: ICP value enters the swap system but no SNS tokens are minted in return. The total SNS tokens distributed will be less than `sns_token_e8s`, and the undistributed remainder stays in the swap canister with no defined recovery path for the affected buyer. [5](#0-4) 

### Likelihood Explanation

This is reachable by any unprivileged ingress sender who participates in an SNS swap. The condition is triggered when:

- `amount_icp_e8s * sns_token_e8s < total_icp_e8s`

For example, if `sns_token_e8s = 1_000_000` (10 SNS tokens) and `total_icp_e8s = 10_000_000_000` (100 ICP), any buyer contributing fewer than `10_000` ICP e8s (0.0001 ICP) gets zero SNS tokens. The `min_participant_icp_e8s` validation in `Params::validate` is supposed to prevent this, but the check uses `max_icp_e8s` (the cap), not the actual `total_icp_e8s` at finalization time. If the swap closes with fewer ICP than the maximum, the actual ratio is more favorable to the attacker, and the minimum participant check may not be tight enough to prevent zero allocations. [6](#0-5) 

Additionally, the Neurons' Fund path has the same issue: [7](#0-6) 

### Recommendation

1. **Add a zero-check after `Swap::scale`**: If `amount_sns_e8s == 0`, skip the buyer, log an error, and mark them for ICP refund rather than proceeding with the sweep.
2. **Tighten the `min_participant_icp_e8s` validation** to use the actual minimum possible `total_icp_e8s` (i.e., `min_direct_participation_icp_e8s`) rather than `max_icp_e8s`, ensuring the minimum contribution always yields at least 1 SNS e8s per neuron basket slot.
3. **Add a guard in `create_sns_neuron_basket_for_direct_participant`**: Return an error if `amount_sns_token_e8s == 0`. [8](#0-7) 

### Proof of Concept

**Setup:**
- `sns_token_e8s = 100` (1 μSNS token)
- `max_icp_e8s = 1_000_000_000` (10 ICP)
- `min_participant_icp_e8s = 1_000` (passes validation: `1_000 * 100 / 1_000_000_000 = 0`, which is `>= neuron_basket_count * (min_stake + fee)` only if those are also 0 — but with a very small SNS pool this can pass)
- Swap closes with `total_icp_e8s = 1_000_000_000`

**Victim participates with `amount_icp_e8s = 999`:**

```
scale(999, 100, 1_000_000_000)
= (999 * 100) / 1_000_000_000
= 99_900 / 1_000_000_000
= 0  (integer floor)
```

`amount_sns_e8s = 0` → `generate_vesting_schedule(0)` → `apportion_approximately_equally(0, 3)` → `[0, 0, 0]` → 3 neuron recipes with `amount_e8s = 0` created successfully.

ICP sweep transfers `999 - fee` ICP to SNS Governance. Victim's ICP is gone. Neurons with 0 stake cannot be claimed. No refund is issued. [9](#0-8) [10](#0-9)

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

**File:** rs/sns/swap/src/swap.rs (L848-852)
```rust
            let amount_sns_e8s = Swap::scale(
                buyer_state.amount_icp_e8s(),
                sns_being_offered_e8s,
                total_participant_icp_e8s,
            );
```

**File:** rs/sns/swap/src/swap.rs (L858-882)
```rust
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

**File:** rs/sns/swap/src/swap.rs (L920-924)
```rust
                    let amount_sns_e8s = Swap::scale(
                        neurons_fund_neuron.amount_icp_e8s,
                        sns_being_offered_e8s,
                        total_participant_icp_e8s,
                    );
```

**File:** rs/sns/swap/src/swap.rs (L3299-3308)
```rust
fn create_sns_neuron_basket_for_direct_participant(
    buyer_principal: &PrincipalId,
    amount_sns_token_e8s: u64,
    neuron_basket_construction_parameters: &NeuronBasketConstructionParameters,
    memo_offset: u64,
) -> Result<Vec<SnsNeuronRecipe>, String> {
    let mut recipes = vec![];

    let vesting_schedule =
        neuron_basket_construction_parameters.generate_vesting_schedule(amount_sns_token_e8s)?;
```

**File:** rs/sns/swap/src/types.rs (L346-367)
```rust
        let min_participant_sns_e8s = self.min_participant_icp_e8s as u128
            * self.sns_token_e8s as u128
            / self.max_icp_e8s as u128;

        let min_participant_icp_e8s_big_enough = min_participant_sns_e8s
            >= neuron_basket_count * (neuron_minimum_stake_e8s + transaction_fee_e8s) as u128;

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
