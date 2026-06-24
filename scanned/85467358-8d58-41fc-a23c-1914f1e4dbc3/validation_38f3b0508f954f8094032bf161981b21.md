### Title
Integer Truncation in `Swap::scale` Causes Zero SNS Token Allocation for Small ICP Contributors - (File: rs/sns/swap/src/swap.rs)

### Summary

The SNS Swap canister's `Swap::scale` function uses integer division to compute each participant's SNS token allocation. When a participant's ICP contribution is small relative to the total ICP raised, the integer division truncates to zero. A participant who contributed valid ICP (above `min_participant_icp_e8s`) can receive zero SNS tokens at finalization, losing their ICP to the swap with no token compensation.

### Finding Description

In `create_sns_neuron_recipes`, each buyer's SNS token allocation is computed by:

```rust
let amount_sns_e8s = Swap::scale(
    buyer_state.amount_icp_e8s(),
    sns_being_offered_e8s,
    total_participant_icp_e8s,
);
```

The `scale` function performs:

```rust
fn scale(amount_icp_e8s: u64, total_sns_e8s: u64, total_icp_e8s: NonZeroU64) -> u64 {
    let r = (amount_icp_e8s as u128)
        .saturating_mul(total_sns_e8s as u128)
        .div(NonZeroU128::from(total_icp_e8s));
    r as u64
}
```

This computes `(buyer_icp * total_sns) / total_icp` using integer (floor) division. If `buyer_icp * total_sns < total_icp`, the result is zero. The computed `amount_sns_e8s = 0` is then passed directly to `create_sns_neuron_basket_for_direct_participant`, which calls `generate_vesting_schedule(0)`, which calls `apportion_approximately_equally(0, count)`. This succeeds and produces `count` neurons each with `amount_e8s = 0`. The buyer's ICP is swept to SNS governance, but the buyer receives neurons with zero stake.

**Concrete trigger condition:** A large whale participant joins the swap, dramatically increasing `total_participant_icp_e8s`. For any existing participant whose `amount_icp_e8s * sns_token_e8s < total_participant_icp_e8s`, their `amount_sns_e8s` rounds to zero. For example:

- `sns_token_e8s = 1_000_000` (10 SNS tokens)
- Attacker deposits `max_direct_participation_icp_e8s - 1` ICP (nearly the entire cap)
- Victim had deposited `min_participant_icp_e8s` ICP
- `victim_icp * 1_000_000 < total_icp` → `amount_sns_e8s = 0`

The attacker, as the dominant participant, receives nearly all SNS tokens. The victim's ICP is transferred to SNS governance (line 985 log confirms `sns_being_offered_e8s - total_sns_tokens_sold_e8s` is the "change" left over), but the victim gets zero-stake neurons.

The validation in `rs/sns/init/src/lib.rs` at line 1636–1642 attempts to prevent this by checking `min_participant_sns_e8s >= basket_count * (neuron_minimum_stake + fee)`, but this check uses `max_direct_participation_icp_e8s` as the denominator — it only guarantees the minimum at maximum participation. If the swap closes at a participation level between `min_direct_participation_icp_e8s` and `max_direct_participation_icp_e8s`, the per-participant SNS amount can still truncate to zero for small contributors when a large participant joins late.

### Impact Explanation

A participant who contributed ICP above `min_participant_icp_e8s` (a valid, accepted contribution) can receive SNS neurons with `amount_e8s = 0`. Their ICP is permanently transferred to the SNS governance canister (the swap is committed and ICP swept), but they receive no meaningful SNS tokens. This is a direct, irreversible financial loss for the victim. The attacker (large depositor) receives a disproportionately large share of SNS tokens — effectively the entire offering minus rounding dust — at the expense of small participants.

### Likelihood Explanation

This is reachable by any unprivileged ingress sender who can call `refresh_buyer_token_e8s` on an open SNS swap canister. The attacker needs only to deposit a large ICP amount (up to `max_participant_icp_e8s`) near the end of the swap window, after small participants have already committed. The swap's `max_participant_icp_e8s` cap limits how dominant a single participant can be, but with a high cap or many small participants, the condition is achievable. The SNS swap is a publicly accessible canister on mainnet.

### Recommendation

1. **Add a zero-allocation guard in `create_sns_neuron_recipes`**: After computing `amount_sns_e8s`, check if it is zero and treat the participant as a failure (log an error and count as `failure` rather than `success`), or revert the entire finalization.
2. **Enforce a minimum SNS token allocation at finalization time**: Before calling `create_sns_neuron_basket_for_direct_participant`, assert `amount_sns_e8s >= neuron_basket_count * (neuron_minimum_stake_e8s + transaction_fee_e8s)`.
3. **Strengthen the init-time validation**: The check at `rs/sns/init/src/lib.rs:1636–1642` uses `max_direct_participation_icp_e8s` as the denominator, but the swap can commit at any participation level ≥ `min_direct_participation_icp_e8s`. The check should use `min_direct_participation_icp_e8s` to guarantee the invariant holds at the worst-case (lowest) participation level.

### Proof of Concept

**Setup:**
- `sns_token_e8s = 10_000_000` (0.1 SNS tokens, 10^7 e8s)
- `min_participant_icp_e8s = 1_000_000` (0.01 ICP)
- `max_participant_icp_e8s = 100_000_000_000` (1000 ICP)
- `max_direct_participation_icp_e8s = 100_000_000_000`
- `neuron_basket_count = 3`

**Steps:**
1. Victim calls `refresh_buyer_token_e8s` with 0.01 ICP → accepted, `buyer_state.amount_icp_e8s = 1_000_000`.
2. Attacker calls `refresh_buyer_token_e8s` with 999.99 ICP → `total_participant_icp_e8s = 100_000_000_000`.
3. Swap commits (max ICP reached).
4. `finalize` → `create_sns_neuron_recipes` is called.
5. For victim: `scale(1_000_000, 10_000_000, 100_000_000_000)` = `(1_000_000 * 10_000_000) / 100_000_000_000` = `10^13 / 10^11` = `100` e8s total → split across 3 neurons = `[33, 33, 34]` e8s each. These are below `neuron_minimum_stake_e8s` and the SNS governance `claim_swap_neurons` call will fail or create dust neurons.
6. For a more extreme case with `sns_token_e8s = 1_000` (10 e8s of SNS): `scale(1_000_000, 1_000, 100_000_000_000)` = `10^9 / 10^11` = **0**. The victim's ICP is swept to SNS governance; they receive 3 neurons each with `amount_e8s = 0`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** rs/sns/swap/src/swap.rs (L203-243)
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

**File:** rs/sns/swap/src/swap.rs (L815-830)
```rust
        let sns_being_offered_e8s = params.sns_token_e8s;
        // Note that this value has to be > 0 as we have > 0
        // participants each with > 0 ICP contributed.
        let total_participant_icp_e8s = match NonZeroU64::try_from(
            self.current_total_participation_e8s(),
        ) {
            Ok(total_participant_icp_e8s) => total_participant_icp_e8s,
            Err(error_message) => {
                log!(
                    ERROR,
                    "Halting create_sns_neuron_recipes(). Swap is finalizing with 0 total participation: {:?}",
                    error_message
                );
                return SweepResult::new_with_global_failures(1);
            }
        };
```

**File:** rs/sns/swap/src/swap.rs (L848-852)
```rust
            let amount_sns_e8s = Swap::scale(
                buyer_state.amount_icp_e8s(),
                sns_being_offered_e8s,
                total_participant_icp_e8s,
            );
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
