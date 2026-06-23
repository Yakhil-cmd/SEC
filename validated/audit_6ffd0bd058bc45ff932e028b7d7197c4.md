### Title
SNS Swap `validate_participation_constraints` Uses Insufficient Denominator in Floor Division, Allowing Swap Configurations Where Participants Receive Fewer SNS Tokens Than Required for Neuron Formation - (`File: rs/sns/init/src/lib.rs`)

---

### Summary

The `validate_participation_constraints` function in `rs/sns/init/src/lib.rs` uses floor (integer) division with `max_direct_participation_icp_e8s` as the denominator to verify that `min_participant_icp_e8s` is large enough to guarantee each participant receives sufficient SNS tokens to form their neuron basket. However, the actual swap token distribution in `Swap::scale` (`rs/sns/swap/src/swap.rs`) divides by `total_participant_icp_e8s`, which includes **Neurons' Fund (NF) participation** and can therefore be strictly larger than `max_direct_participation_icp_e8s`. The validation passes, but at finalization each direct participant receives fewer SNS tokens than the minimum required to form neurons, causing neuron recipe creation to fail and participants to lose their SNS token allocation.

---

### Finding Description

**Root cause — validation denominator mismatch:**

`validate_participation_constraints` computes the minimum SNS tokens a participant will receive as:

```rust
// rs/sns/init/src/lib.rs:1636-1638
let min_participant_sns_e8s = min_participant_icp_e8s as u128
    * initial_swap_amount_e8s as u128
    / max_direct_participation_icp_e8s as u128;   // ← only direct ICP
```

It then checks:

```rust
// rs/sns/init/src/lib.rs:1640-1642
let min_participant_icp_e8s_big_enough = min_participant_sns_e8s
    >= neuron_basket_construction_parameters_count as u128
        * (neuron_minimum_stake_e8s + sns_transaction_fee_e8s) as u128;
``` [1](#0-0) 

**Actual swap computation — larger denominator:**

At finalization, `Swap::scale` computes each participant's SNS allocation using the **actual total ICP**, which includes NF:

```rust
// rs/sns/swap/src/swap.rs:742-751
fn scale(amount_icp_e8s: u64, total_sns_e8s: u64, total_icp_e8s: NonZeroU64) -> u64 {
    let r = (amount_icp_e8s as u128)
        .saturating_mul(total_sns_e8s as u128)
        .div(NonZeroU128::from(total_icp_e8s));   // ← direct + NF ICP
    r as u64
}
``` [2](#0-1) 

`total_icp_e8s` = `current_direct_participation_e8s()` + `current_neurons_fund_participation_e8s()`, which can exceed `max_direct_participation_icp_e8s`.

**Compounding floor division in neuron basket splitting:**

The result of `Swap::scale` is then split across `neuron_basket_count` neurons via `apportion_approximately_equally`, which also uses floor division:

```rust
// rs/sns/swap/src/swap.rs:848-852
let amount_sns_e8s = Swap::scale(
    buyer_state.amount_icp_e8s(),
    sns_being_offered_e8s,
    total_participant_icp_e8s,
);
``` [3](#0-2) 

```rust
// rs/sns/swap/src/swap.rs:191-207
pub fn apportion_approximately_equally(total: u64, len: u64) -> Result<Vec<u64>, String> {
    let quotient = total.checked_div(len)...;   // floor division
    ...
}
``` [4](#0-3) 

**Concrete proof-of-concept:**

| Parameter | Value |
|---|---|
| `min_participant_icp_e8s` | 5 |
| `initial_swap_amount_e8s` | 10 |
| `max_direct_participation_icp_e8s` | 7 |
| NF participation | 3 ICP |
| `total_participant_icp_e8s` | 10 |
| `neuron_basket_count` | 2 |
| `neuron_minimum_stake_e8s` | 3 |
| `transaction_fee_e8s` | 0 |

**Validation (passes):**
```
min_participant_sns_e8s = 5 * 10 / 7 = 7  (floor)
7 >= 2 * (3 + 0) = 7 >= 6  → true ✓
```

**Actual swap finalization (fails):**
```
amount_sns_e8s = 5 * 10 / 10 = 5  (floor, larger denominator)
apportion_approximately_equally(5, 2) = [3, 2]
neuron[1].stake = 2 < neuron_minimum_stake_e8s (3)  → FAIL
```

The neuron recipe creation for the participant fails at:

```rust
// rs/sns/swap/src/swap.rs:858-882
match create_sns_neuron_basket_for_direct_participant(
    &buyer_principal,
    amount_sns_e8s,
    neuron_basket_construction_parameters,
    NEURON_BASKET_MEMO_RANGE_START,
) {
    Ok(...) => { ... }
    Err(error_message) => {
        log!(ERROR, ...);
        sweep_result.failure += neuron_basket_construction_parameters.count as u32;
        continue;   // participant's SNS tokens are never distributed
    }
};
``` [5](#0-4) 

The participant's ICP has already been swept to SNS governance, but their SNS neuron recipes are never created.

---

### Impact Explanation

Direct swap participants who contributed the minimum allowed ICP (`min_participant_icp_e8s`) lose their SNS token allocation when Neurons' Fund participation causes `total_participant_icp_e8s` to exceed `max_direct_participation_icp_e8s`. Their ICP is transferred to SNS governance (irreversible), but no SNS neurons are created for them. This is a **ledger conservation bug**: ICP is burned/transferred but the corresponding SNS tokens are never minted to the participant.

---

### Likelihood Explanation

Any SNS swap that:
1. Enables Neurons' Fund participation (`neurons_fund_participation = true`), and
2. Sets `min_participant_icp_e8s` at or near the boundary value that just passes `validate_participation_constraints`

will trigger this condition whenever NF actually participates. NF participation is common in SNS swaps and is determined at finalization time, not at initialization. The attacker-controlled entry path is simply calling `refresh_buyer_tokens` with `min_participant_icp_e8s` and waiting for NF to participate.

---

### Recommendation

The denominator in `validate_participation_constraints` must account for the maximum possible total ICP, including Neurons' Fund:

```rust
// rs/sns/init/src/lib.rs
let max_total_participation_icp_e8s = max_direct_participation_icp_e8s
    .saturating_add(max_neurons_fund_participation_icp_e8s.unwrap_or(0));

let min_participant_sns_e8s = min_participant_icp_e8s as u128
    * initial_swap_amount_e8s as u128
    / max_total_participation_icp_e8s as u128;
```

Similarly, `Params::validate` in `rs/sns/swap/src/types.rs` should use the same total-ICP denominator: [6](#0-5) 

---

### Proof of Concept

Entry path for an unprivileged user:

1. An SNS is initialized with `neurons_fund_participation = true` and `min_participant_icp_e8s` set to the boundary value that just passes `validate_participation_constraints` using `max_direct_participation_icp_e8s` as the denominator.
2. The swap opens. A direct participant calls `refresh_buyer_tokens` contributing exactly `min_participant_icp_e8s`.
3. Neurons' Fund participates (triggered automatically by NNS governance), increasing `total_participant_icp_e8s` beyond `max_direct_participation_icp_e8s`.
4. The swap commits. `create_sns_neuron_recipes` is called. `Swap::scale` computes `amount_sns_e8s` using the larger denominator, producing a value smaller than the validation assumed.
5. `apportion_approximately_equally` splits the reduced amount across `neuron_basket_count` neurons; at least one neuron falls below `neuron_minimum_stake_e8s`.
6. `create_sns_neuron_basket_for_direct_participant` returns `Err`, the participant's neuron recipes are not created, and `sweep_result.failure` is incremented.
7. The participant's ICP has already been transferred to SNS governance; they receive no SNS tokens. [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

**File:** rs/sns/init/src/lib.rs (L1627-1655)
```rust
        // (7)
        if neuron_minimum_stake_e8s <= sns_transaction_fee_e8s {
            return Err(format!(
                "Error: neuron_minimum_stake_e8s ({neuron_minimum_stake_e8s}) is too small. It needs to be \
                 greater than the transaction fee ({sns_transaction_fee_e8s} e8s)"
            ));
        }

        // (8)
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
        }
```

**File:** rs/sns/swap/src/swap.rs (L203-207)
```rust
pub fn apportion_approximately_equally(total: u64, len: u64) -> Result<Vec<u64>, String> {
    let quotient = total
        .checked_div(len)
        .ok_or_else(|| format!("Unable to divide total={total} by len={len}"))?;
    let remainder = total % len; // For unsigned integers, % cannot overflow.
```

**File:** rs/sns/swap/src/swap.rs (L738-751)
```rust
    /// Computes `amount_icp_e8s` scaled by (`total_sns_e8s` divided by
    /// `total_icp_e8s`), but perform the computation in integer space
    /// by computing `(amount_icp_e8s * total_sns_e8s) /
    /// total_icp_e8s` in 128 bit space.
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

**File:** rs/sns/swap/src/swap.rs (L837-883)
```rust
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
        }
```

**File:** rs/sns/swap/src/types.rs (L346-348)
```rust
        let min_participant_sns_e8s = self.min_participant_icp_e8s as u128
            * self.sns_token_e8s as u128
            / self.max_icp_e8s as u128;
```
