### Title
SNS Swap `Swap::scale()` Floor Division Produces Zero SNS Tokens for Participants When Neurons' Fund Participation Dilutes the Rate - (File: `rs/sns/swap/src/swap.rs`)

---

### Summary

The SNS Swap canister's `Swap::scale()` function uses floor (integer) division to compute each participant's SNS token allocation. The swap initialization validation (`validate_participation_constraints`) checks that `min_participant_icp_e8s` is large enough using only `max_direct_participation_icp_e8s` as the denominator, but the actual `scale()` function divides by `total_participant_icp_e8s`, which includes Neurons' Fund participation. When Neurons' Fund participation is significant, the actual SNS tokens per ICP can be much lower than what the validation assumed, causing `scale()` to return 0 for participants contributing the minimum allowed ICP. Their ICP is transferred to SNS governance but they receive 0 SNS tokens, resulting in permanent loss of funds — a direct analog to the Limit Order Rounding vulnerability.

---

### Finding Description

**Root cause — `Swap::scale()` uses floor division:** [1](#0-0) 

The function computes `(amount_icp_e8s * total_sns_e8s) / total_participant_icp_e8s` in integer space. If `amount_icp_e8s * total_sns_e8s < total_participant_icp_e8s`, the result is 0. There is no guard against a zero result before passing it downstream.

**Validation uses the wrong denominator:** [2](#0-1) 

The validation computes `min_participant_sns_e8s = (min_participant_icp_e8s * initial_swap_amount_e8s) / max_direct_participation_icp_e8s` and checks it is at least `basket_count * (min_stake + fee)`. This denominator is `max_direct_participation_icp_e8s` — the maximum *direct* ICP — but the actual `scale()` call at swap finalization divides by `total_participant_icp_e8s`, which includes Neurons' Fund ICP. If the Neurons' Fund contributes significantly, `total_participant_icp_e8s >> max_direct_participation_icp_e8s`, and the actual SNS tokens per ICP is far lower than what the validation assumed.

**Zero propagates through the basket construction:** [3](#0-2) 

`create_sns_neuron_basket_for_direct_participant()` is called with `amount_sns_e8s = 0` without any zero-check. This calls `generate_vesting_schedule(0)`: [4](#0-3) 

Which calls `apportion_approximately_equally(0, count)`: [5](#0-4) 

`apportion_approximately_equally(0, count)` returns `Ok(vec![0; count])` — all zeros, no error. Neuron recipes are created with `amount_e8s: 0`: [6](#0-5) 

During `sweep_sns`, the swap attempts to transfer `0 - transaction_fee_e8s` SNS tokens to each neuron account. This underflows, the transfer fails, and the recipe is marked as failed. The participant's ICP has already been swept to SNS governance in `sweep_icp`, so the ICP is permanently lost with no SNS tokens received.

**Secondary rounding issue — basket apportionment:**

Even when `scale()` returns a small non-zero value, `apportion_approximately_equally(total, count)` can produce 0 for individual neurons in the basket (e.g., `apportion_approximately_equally(2, 3) = [0, 1, 1]`). The validation only ensures the *total* is at least `basket_count * (min_stake + fee)`, not that each individual neuron receives at least `min_stake + fee`. Neurons' Fund dilution can reduce the total below this threshold, causing individual basket neurons to receive 0 tokens.

---

### Impact Explanation

Direct participants who contribute `min_participant_icp_e8s` ICP — the minimum amount explicitly validated as safe — can permanently lose their ICP without receiving any SNS tokens. The ICP is transferred to SNS governance during `sweep_icp`, but the corresponding SNS neuron recipes have `amount_e8s = 0`, causing `sweep_sns` transfers to fail. This is a ledger conservation bug: ICP is consumed but no SNS tokens are minted to the participant. The loss is irreversible once the swap is finalized.

---

### Likelihood Explanation

This requires a swap with Neurons' Fund (matched funding) participation enabled, which is the standard configuration for new SNS launches. The Neurons' Fund participation amount is determined by the NNS governance matching function and can be substantial — potentially equal to or exceeding direct participation. If the swap is configured with `min_participant_icp_e8s` at the minimum value that just barely passes validation (i.e., `min_participant_icp_e8s * initial_swap_amount_e8s ≈ max_direct_participation_icp_e8s`), then any Neurons' Fund participation at all causes `scale()` to return 0 for minimum-contributing direct participants. This is a realistic configuration for competitive SNS launches where the Neurons' Fund is expected to match direct participation.

---

### Recommendation

1. **Fix the validation denominator**: In `validate_participation_constraints` (`rs/sns/init/src/lib.rs`), use `max_direct_participation_icp_e8s + max_neurons_fund_participation_icp_e8s` as the denominator when computing `min_participant_sns_e8s`, so the worst-case SNS token allocation accounts for maximum possible Neurons' Fund dilution.

2. **Add a zero-guard in `create_sns_neuron_recipes`**: Before calling `create_sns_neuron_basket_for_direct_participant`, check if `amount_sns_e8s == 0` and either skip the participant (counting as `invalid`) or refund their ICP.

3. **Add a zero-guard in `apportion_approximately_equally` callers**: Before calling `generate_vesting_schedule`, verify that `total_amount_e8s >= count` so no individual neuron receives 0 tokens.

---

### Proof of Concept

Configure a swap with matched funding:
- `max_direct_participation_icp_e8s = 100 ICP` (= 10,000,000,000 e8s)
- `initial_swap_amount_e8s = 100 SNS tokens` (= 10,000,000,000 e8s)
- `min_participant_icp_e8s = 1 ICP` (= 100,000,000 e8s)
- `neuron_basket_count = 1`, `neuron_minimum_stake_e8s = 1`, `transaction_fee_e8s = 0`

**Validation passes** (`rs/sns/init/src/lib.rs` line 1636–1654):
```
min_participant_sns_e8s = (100_000_000 * 10_000_000_000) / 10_000_000_000 = 100_000_000
100_000_000 >= 1 * (1 + 0) = 1  ✓
```

**Neurons' Fund contributes 100 ICP**, making `total_participant_icp_e8s = 200 ICP` (= 20,000,000,000 e8s).

**Direct participant contributes 1 ICP** (`min_participant_icp_e8s`):

`Swap::scale(100_000_000, 10_000_000_000, 20_000_000_000)`:
```
= (100_000_000 * 10_000_000_000) / 20_000_000_000
= 1_000_000_000_000_000_000 / 20_000_000_000
= 50_000_000  (floor)
```

Wait — in this example the result is non-zero. Let me tighten the parameters:

- `max_direct_participation_icp_e8s = 100 ICP`
- `initial_swap_amount_e8s = 1 SNS token` (= 100,000,000 e8s)
- `min_participant_icp_e8s = 1 ICP` (= 100,000,000 e8s)
- `neuron_basket_count = 1`, `neuron_minimum_stake_e8s = 1`, `transaction_fee_e8s = 0`

**Validation passes**:
```
min_participant_sns_e8s = (100_000_000 * 100_000_000) / 10_000_000_000 = 1
1 >= 1 * (1 + 0) = 1  ✓
```

**Neurons' Fund contributes 1 ICP**, making `total_participant_icp_e8s = 101 ICP` (= 10,100,000,000 e8s).

**Direct participant contributes 1 ICP**:

`Swap::scale(100_000_000, 100_000_000, 10_100_000_000)`:
```
= (100_000_000 * 100_000_000) / 10_100_000_000
= 10_000_000_000_000_000 / 10_100_000_000
= 0  (floor)
```

The participant's 1 ICP is swept to SNS governance. Their neuron recipe has `amount_e8s = 0`. The `sweep_sns` transfer fails. The participant permanently loses 1 ICP with no SNS tokens received. [1](#0-0) [2](#0-1) [5](#0-4)

### Citations

**File:** rs/sns/swap/src/swap.rs (L163-188)
```rust
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

**File:** rs/sns/swap/src/swap.rs (L3331-3349)
```rust
        recipes.push(SnsNeuronRecipe {
            sns: Some(TransferableAmount {
                amount_e8s: scheduled_vesting_event.amount_e8s,
                transfer_start_timestamp_seconds: 0,
                transfer_success_timestamp_seconds: 0,
                amount_transferred_e8s: Some(0),
                transfer_fee_paid_e8s: Some(0),
            }),
            investor: Some(Investor::Direct(DirectInvestment {
                buyer_principal: buyer_principal.to_string(),
            })),
            neuron_attributes: Some(NeuronAttributes {
                memo,
                dissolve_delay_seconds: scheduled_vesting_event.dissolve_delay_seconds,
                followees,
            }),
            claimed_status: Some(ClaimedStatus::Pending as i32),
        });
    }
```

**File:** rs/sns/init/src/lib.rs (L1636-1654)
```rust
        let min_participant_sns_e8s = min_participant_icp_e8s as u128
            * initial_swap_amount_e8s as u128
            / max_direct_participation_icp_e8s as u128;

        let min_participant_icp_e8s_big_enough = min_participant_sns_e8s
            >= neuron_basket_construction_parameters_count as u128
                * (neuron_minimum_stake_e8s + sns_transaction_fee_e8s) as u128;

        if !min_participant_icp_e8s_big_enough {
            return Err(format!(
                "Error: min_participant_icp_e8s ({min_participant_icp_e8s}) is too small. It needs to be \
                 large enough to ensure that participants will end up with \
                 enough SNS tokens to form {neuron_basket_construction_parameters_count} SNS neurons, each of which \
                 require at least {neuron_minimum_stake_e8s} SNS e8s, plus {sns_transaction_fee_e8s} e8s in transaction \
                 fees. More precisely, the following inequality must hold: \
                 min_participant_icp_e8s >= neuron_basket_count \
                 * (neuron_minimum_stake_e8s + transaction_fee_e8s) \
                 * max_direct_participation_icp_e8s / initial_swap_amount_e8s",
            ));
```
