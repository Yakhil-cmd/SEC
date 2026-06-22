### Title
SNS Swap `create_sns_neuron_recipes` Distributes More SNS Tokens Than Available Due to Per-Participant Integer Division Rounding - (`File: rs/sns/swap/src/swap.rs`)

### Summary

The SNS Swap canister's `create_sns_neuron_recipes` function allocates SNS tokens to each participant by independently applying integer floor-division (`scale`) per participant. Because each individual division truncates (rounds down), the sum of all per-participant allocations is always ≤ `sns_being_offered_e8s`. This is the correct direction for conservation. However, the analog to the Astaria bug exists in the **neuron basket splitting** step: `apportion_approximately_equally` distributes a participant's `amount_sns_e8s` across `count` neurons such that `sum(basket) == amount_sns_e8s` exactly, and each neuron's `TransferableAmount.amount_e8s` is set to the basket slice. During `sweep_sns`, each recipe's `transfer_helper` sends `amount_e8s - fee` to the ledger subaccount. The `amount_e8s` stored in each recipe is the **pre-fee** amount, but the SNS ledger balance that the Swap canister actually holds is `sns_being_offered_e8s` — which was never reduced by the per-transfer fees. Consequently, the Swap canister attempts to transfer a total of `Σ(amount_e8s_i - fee)` from a balance of `sns_being_offered_e8s`, where `Σ(amount_e8s_i) ≤ sns_being_offered_e8s` but the fees are paid **on top of** the stored amounts, meaning the canister must hold `Σ(amount_e8s_i)` (not `Σ(amount_e8s_i) - N*fee`). The actual conservation issue is the inverse: the Swap canister holds exactly `sns_being_offered_e8s` SNS tokens, but distributes `Σ(amount_e8s_i - fee_per_recipe)` to participants, silently burning `N * fee` tokens as ledger fees — tokens that were never accounted for in the swap parameters and are not returned to the SNS treasury.

### Finding Description

In `create_sns_neuron_recipes`, each participant's SNS allocation is computed via `Swap::scale`:

```rust
// rs/sns/swap/src/swap.rs:848-852
let amount_sns_e8s = Swap::scale(
    buyer_state.amount_icp_e8s(),
    sns_being_offered_e8s,
    total_participant_icp_e8s,
);
``` [1](#0-0) 

This `amount_sns_e8s` is stored verbatim in each `SnsNeuronRecipe`'s `TransferableAmount.amount_e8s` field (split across the neuron basket via `apportion_approximately_equally`). [2](#0-1) 

During `sweep_sns`, `transfer_helper` is called for each recipe:

```rust
// rs/sns/swap/src/types.rs:626-633
let result = ledger
    .transfer_funds(
        amount.get_e8s().saturating_sub(fee.get_e8s()),
        fee.get_e8s(),
        subaccount,
        *dst,
        0,
    )
    .await;
``` [3](#0-2) 

The ledger deducts `amount_e8s` (the full stored amount) from the Swap canister's SNS balance: `amount_e8s - fee` goes to the recipient neuron subaccount, and `fee` is burned (or collected by the fee collector). The Swap canister's SNS balance must therefore cover `Σ(amount_e8s_i)` across all recipes, not `Σ(amount_e8s_i - fee_i)`.

The Swap canister holds exactly `sns_being_offered_e8s` SNS tokens. Since `Σ(amount_e8s_i) ≤ sns_being_offered_e8s` (due to floor division), the transfers succeed. But the **effective amount received by participants** is `Σ(amount_e8s_i) - N * fee`, where `N` is the total number of neuron recipes. The difference `N * fee` is silently burned as ledger fees and is never returned to the SNS treasury or governance. This is a ledger conservation bug: the SNS project loses `N * fee` tokens that were supposed to be part of the swap offering, with no accounting or recovery path.

The `sns_being_offered_e8s` parameter is set at swap initialization and represents the total SNS tokens offered. The swap parameters validation (`Params::validate`) checks that `min_participant_sns_e8s >= neuron_basket_count * (neuron_minimum_stake_e8s + transaction_fee_e8s)`, which ensures individual neurons are viable, but does **not** ensure the total fee burden across all participants is accounted for in the offered token amount. [4](#0-3) 

### Impact Explanation

- **Token conservation violation**: For a swap with `P` participants each receiving `B` neurons in their basket, `P * B * fee` SNS tokens are burned as ledger fees. These tokens were part of `sns_being_offered_e8s` but are not distributed to participants and not returned to the SNS treasury.
- **Participants receive less than the swap rate implies**: The effective SNS tokens received per ICP is `(sns_being_offered_e8s - P*B*fee) / total_icp_e8s`, not `sns_being_offered_e8s / total_icp_e8s` as advertised.
- **Scale**: With 10,000 participants, 3 neurons per basket, and a fee of 10,000 e8s per transfer, `10,000 * 3 * 10,000 = 300,000,000 e8s = 3 SNS tokens` are silently burned. For high-value SNS tokens or large swaps, this is material.
- **No recovery**: The burned tokens are gone; there is no mechanism to reclaim them.

### Likelihood Explanation

This affects every committed SNS swap with a non-zero ledger fee. The SNS token ledger always has a non-zero `transaction_fee_e8s` (set in `Init`). Every `sweep_sns` call pays this fee per recipe. The condition is always triggered in production swaps. The entry path is fully unprivileged: any user participating in an open SNS swap triggers this path upon finalization. [5](#0-4) 

### Recommendation

Account for the total fee burden when computing per-participant SNS allocations. Two approaches:

1. **Reduce `sns_being_offered_e8s` by the expected total fees before distribution**: Compute `effective_sns_e8s = sns_being_offered_e8s - (total_recipe_count * fee)` and use this as the basis for `scale()` calls.
2. **Adjust `amount_e8s` in each recipe to be the net amount** (i.e., what the participant actually receives), and transfer exactly `amount_e8s` with fee paid separately from a fee reserve. This matches the Astaria recommendation: only transfer `(1 - fee_fraction) * T` to recipients.

Additionally, the swap parameter validation should enforce that `sns_token_e8s >= expected_total_distribution + expected_total_fees`.

### Proof of Concept

Consider a swap with:
- `sns_being_offered_e8s = 1_000_000_000` (10 SNS tokens at 8 decimals)
- 2 direct participants, each contributing 50 ICP
- `neuron_basket_count = 3`
- `transaction_fee_e8s = 10_000`

Step 1 — `create_sns_neuron_recipes`:
- Each participant gets `scale(50*E8, 1_000_000_000, 100*E8) = 500_000_000` SNS e8s
- Each basket of 3 neurons gets `apportion_approximately_equally(500_000_000, 3)` ≈ `[166_666_667, 166_666_667, 166_666_666]`
- Total recipes: 6, total `amount_e8s` stored: `1_000_000_000` [6](#0-5) 

Step 2 — `sweep_sns` calls `transfer_helper` for each of the 6 recipes:
- Each transfer sends `amount_e8s - 10_000` to the neuron subaccount, paying `10_000` as fee
- Total sent to participants: `1_000_000_000 - 6 * 10_000 = 999_940_000`
- Total fees burned: `60_000` SNS e8s (0.0006 SNS tokens) [7](#0-6) 

The Swap canister's SNS balance goes from `1_000_000_000` to `0` (all tokens consumed), but participants collectively received only `999_940_000`. The `60_000` e8s difference is burned as ledger fees, unaccounted for in the swap economics. The SNS treasury receives nothing from this shortfall. [8](#0-7)

### Citations

**File:** rs/sns/swap/src/swap.rs (L191-243)
```rust
/// Chops up `total` in to `len` pieces.
///
/// More precisely, result.len() == len. result.sum() == total. Each element of
/// result is approximately equal to the others. However, unless len divides
/// total evenly, the elements of result will inevitably be not equal.
///
/// There are two ways that Err can be returned:
///
///   1. Caller mistake: len == 0
///
///   2. This has a bug. See implementation comments for why we know of know way
///      this can happen, but can detect if it does.
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

**File:** rs/sns/swap/src/swap.rs (L848-852)
```rust
            let amount_sns_e8s = Swap::scale(
                buyer_state.amount_icp_e8s(),
                sns_being_offered_e8s,
                total_participant_icp_e8s,
            );
```

**File:** rs/sns/swap/src/swap.rs (L976-988)
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

        sweep_result
```

**File:** rs/sns/swap/src/swap.rs (L2165-2197)
```rust
    pub async fn sweep_sns(
        &mut self,
        now_fn: fn(bool) -> u64,
        sns_ledger: &dyn ICRC1Ledger,
    ) -> SweepResult {
        if self.lifecycle() != Lifecycle::Committed {
            log!(
                ERROR,
                "Halting sweep_sns(). SNS Tokens cannot be distributed if \
                Lifecycle is not COMMITTED. Current Lifecycle: {:?}",
                self.lifecycle()
            );
            return SweepResult::new_with_global_failures(1);
        }

        let init = match self.init_and_validate() {
            Ok(init) => init,
            Err(error_message) => {
                log!(
                    ERROR,
                    "Halting sweep_sns(). State is missing or corrupted: {:?}",
                    error_message
                );
                return SweepResult::new_with_global_failures(1);
            }
        };

        // The following methods are safe to call since we validated Init in the above block
        let sns_governance = init.sns_governance_or_panic();
        let nns_governance = init.nns_governance_or_panic();
        let sns_transaction_fee_tokens = Tokens::from_e8s(init.transaction_fee_e8s_or_panic());

        let mut sweep_result = SweepResult::default();
```

**File:** rs/sns/swap/src/types.rs (L346-351)
```rust
        let min_participant_sns_e8s = self.min_participant_icp_e8s as u128
            * self.sns_token_e8s as u128
            / self.max_icp_e8s as u128;

        let min_participant_icp_e8s_big_enough = min_participant_sns_e8s
            >= neuron_basket_count * (neuron_minimum_stake_e8s + transaction_fee_e8s) as u128;
```

**File:** rs/sns/swap/src/types.rs (L604-633)
```rust
    pub(crate) async fn transfer_helper(
        &mut self,
        now_fn: fn(bool) -> u64,
        fee: Tokens,
        subaccount: Option<Subaccount>,
        dst: &Account,
        ledger: &dyn ICRC1Ledger,
    ) -> TransferResult {
        let amount = Tokens::from_e8s(self.amount_e8s);
        if amount <= fee {
            // Skip: amount too small...
            return TransferResult::AmountTooSmall;
        }
        if self.transfer_start_timestamp_seconds > 0 {
            // Operation in progress...
            return TransferResult::AlreadyStarted;
        }
        self.transfer_start_timestamp_seconds = now_fn(false);

        // The ICRC1Ledger Trait converts any errors to Err(NervousSystemError).
        // No panics should occur when issuing this transfer.
        let result = ledger
            .transfer_funds(
                amount.get_e8s().saturating_sub(fee.get_e8s()),
                fee.get_e8s(),
                subaccount,
                *dst,
                0,
            )
            .await;
```
