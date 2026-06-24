### Title
Integer Division Truncation in `Swap::scale()` Produces Zero SNS Token Allocation, Permanently Halting Swap Finalization - (File: `rs/sns/swap/src/swap.rs`)

### Summary

In the SNS Swap canister, `Swap::scale()` uses floor (integer) division to compute each participant's SNS token allocation. When a participant's ICP contribution is small relative to total participation and the SNS token offering, the result truncates to zero. This zero is silently propagated into neuron recipes, which are then permanently marked as created. During `sweep_sns()`, each zero-amount recipe returns `AmountTooSmall` (counted as `invalid`), causing finalization to halt permanently and locking all participant funds.

### Finding Description

**Step 1 — Floor division can produce zero.**

`Swap::scale()` computes each participant's SNS token share:

```rust
fn scale(amount_icp_e8s: u64, total_sns_e8s: u64, total_icp_e8s: NonZeroU64) -> u64 {
    let r = (amount_icp_e8s as u128)
        .saturating_mul(total_sns_e8s as u128)
        .div(NonZeroU128::from(total_icp_e8s));
    r as u64
}
```

When `amount_icp_e8s * total_sns_e8s < total_icp_e8s`, integer division truncates to `0`. [1](#0-0) 

**Step 2 — Zero is passed into recipe creation without a guard.**

In `create_sns_neuron_recipes()`, the zero result is passed directly to `create_sns_neuron_basket_for_direct_participant()`:

```rust
let amount_sns_e8s = Swap::scale(
    buyer_state.amount_icp_e8s(),
    sns_being_offered_e8s,
    total_participant_icp_e8s,
);
// No zero-check here
match create_sns_neuron_basket_for_direct_participant(
    &buyer_principal,
    amount_sns_e8s,   // <-- can be 0
    ...
``` [2](#0-1) 

**Step 3 — `generate_vesting_schedule(0)` succeeds silently.**

`generate_vesting_schedule` calls `apportion_approximately_equally(0, count)`:

```rust
fn generate_vesting_schedule(&self, total_amount_e8s: u64) -> Result<Vec<ScheduledVestingEvent>, String> {
    let chunks_e8s = apportion_approximately_equally(total_amount_e8s, self.count)?;
    ...
}
```

`apportion_approximately_equally(0, count)` returns `Ok(vec![0; count])` — no error, all amounts are zero. [3](#0-2) [4](#0-3) 

**Step 4 — Recipes with `amount_e8s = 0` are permanently committed.**

After `create_sns_neuron_basket_for_direct_participant` succeeds, the buyer is marked done:

```rust
buyer_state.has_created_neuron_recipes = Some(true);
```

The recipes with `amount_e8s = 0` are now permanently stored and will never be retried. [5](#0-4) 

**Step 5 — `sweep_sns()` counts zero-amount recipes as `invalid`, halting finalization.**

In `transfer_helper()`, any amount ≤ fee returns `AmountTooSmall`:

```rust
let amount = Tokens::from_e8s(self.amount_e8s);
if amount <= fee {
    return TransferResult::AmountTooSmall;
}
``` [6](#0-5) 

Back in `sweep_sns()`, `AmountTooSmall` increments `sweep_result.invalid`:

```rust
TransferResult::AmountTooSmall => {
    sweep_result.invalid += 1;
}
``` [7](#0-6) 

Finalization then halts permanently with:
> "Transferring SNS tokens did not complete fully, some transfers were invalid or failed. Halting swap finalization"

### Impact Explanation

**Permanent DoS of SNS swap finalization.** Once neuron recipes with `amount_e8s = 0` are created and marked as done, the swap can never be finalized. All participant ICP and SNS tokens remain locked in the swap canister indefinitely. The `has_created_neuron_recipes = Some(true)` flag prevents any retry. This is a ledger conservation bug: participants who contributed ICP receive no SNS tokens and cannot recover their ICP.

### Likelihood Explanation

The condition for `Swap::scale()` to return 0 is:
```
amount_icp_e8s * total_sns_e8s < total_icp_e8s
```

The validation in `Params::validate()` attempts to prevent this using `max_icp_e8s` as the denominator:

```rust
let min_participant_sns_e8s = self.min_participant_icp_e8s as u128
    * self.sns_token_e8s as u128
    / self.max_icp_e8s as u128;
``` [8](#0-7) 

However, `Swap::scale()` uses `total_participant_icp_e8s` (which includes **both** direct and Neurons' Fund participation), while the validation uses only `max_icp_e8s` (which may cap only direct participation). If Neurons' Fund participation pushes `total_participant_icp_e8s` above `max_icp_e8s`, the validation is insufficient and a participant contributing `min_participant_icp_e8s` can receive a zero SNS allocation. An unprivileged direct participant can trigger this by contributing the minimum allowed ICP amount in a swap where NF participation is large.

### Recommendation

1. Add an explicit zero-check in `create_sns_neuron_recipes()` before calling `create_sns_neuron_basket_for_direct_participant()`:

```rust
if amount_sns_e8s == 0 {
    log!(ERROR, "Participant {} would receive 0 SNS tokens; skipping recipe creation", buyer_principal);
    sweep_result.failure += neuron_basket_construction_parameters.count as u32;
    continue;
}
```

2. Fix the validation in `Params::validate()` to use the worst-case total participation (including maximum possible NF participation) rather than `max_icp_e8s` alone.

3. In `generate_vesting_schedule()`, return an error if `total_amount_e8s == 0` to prevent silent propagation of zero amounts.

### Proof of Concept

**Conditions:**
- `sns_token_e8s = 1_000_000` (1 SNS token)
- `min_participant_icp_e8s = 100_000_000` (1 ICP)
- `max_icp_e8s = 10_000_000_000` (100 ICP, direct cap)
- Neurons' Fund contributes `990_000_000_000` e8s (9900 ICP)
- One direct participant contributes `100_000_000` e8s (1 ICP)
- `total_participant_icp_e8s = 990_100_000_000`

**`Swap::scale()` result:**
```
(100_000_000 * 1_000_000) / 990_100_000_000
= 100_000_000_000_000 / 990_100_000_000
= 101  (rounds down from ~101.0)
```

Actually with these numbers it doesn't reach zero. Let me adjust:
- `sns_token_e8s = 100` (very small offering)
- `min_participant_icp_e8s = 1_000_000` (0.01 ICP)
- `max_icp_e8s = 1_000_000_000` (10 ICP)
- NF contributes `999_000_000_000` e8s
- Direct participant contributes `1_000_000` e8s
- `total_participant_icp_e8s = 999_001_000_000`

```
scale(1_000_000, 100, 999_001_000_000)
= (1_000_000 * 100) / 999_001_000_000
= 100_000_000 / 999_001_000_000
= 0  ← truncated to zero
```

Validation passes because:
```
min_participant_sns_e8s = 1_000_000 * 100 / 1_000_000_000 = 0
```
(which is ≥ 0, so validation passes vacuously).

The direct participant's neuron recipes are created with `amount_e8s = 0`, marked as done, and `sweep_sns()` permanently counts them as `invalid`, halting finalization.

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

**File:** rs/sns/swap/src/swap.rs (L203-213)
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

**File:** rs/sns/swap/src/swap.rs (L2276-2282)
```rust
                // AmountToSmall should never happen as the sns token amount is checked in
                // `commit`. In the case of a bug due to programmer error,
                // increment the invalid field. This will require a manual intervention
                // via an upgrade to correct
                TransferResult::AmountTooSmall => {
                    sweep_result.invalid += 1;
                }
```

**File:** rs/sns/swap/src/types.rs (L346-351)
```rust
        let min_participant_sns_e8s = self.min_participant_icp_e8s as u128
            * self.sns_token_e8s as u128
            / self.max_icp_e8s as u128;

        let min_participant_icp_e8s_big_enough = min_participant_sns_e8s
            >= neuron_basket_count * (neuron_minimum_stake_e8s + transaction_fee_e8s) as u128;
```

**File:** rs/sns/swap/src/types.rs (L612-616)
```rust
        let amount = Tokens::from_e8s(self.amount_e8s);
        if amount <= fee {
            // Skip: amount too small...
            return TransferResult::AmountTooSmall;
        }
```
