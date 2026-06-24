### Title
Precision Loss in `Swap::scale` Causes SNS Tokens Permanently Stuck in Swap Canister - (`rs/sns/swap/src/swap.rs`)

---

### Summary

The `Swap::scale` function uses integer (floor) division to compute each participant's SNS token allocation. Because the sum of per-participant floor-divided allocations is strictly less than the total offered when ICP amounts do not divide evenly, a non-zero remainder of SNS tokens is left in the Swap canister's ledger account after finalization with no recovery mechanism.

---

### Finding Description

In `create_sns_neuron_recipes`, each participant's SNS allocation is computed via `Swap::scale`:

```rust
fn scale(amount_icp_e8s: u64, total_sns_e8s: u64, total_icp_e8s: NonZeroU64) -> u64 {
    let r = (amount_icp_e8s as u128)
        .saturating_mul(total_sns_e8s as u128)
        .div(NonZeroU128::from(total_icp_e8s));
    r as u64
}
```

This computes `⌊ amount_icp_e8s × total_sns_e8s / total_icp_e8s ⌋` — truncating the fractional remainder. For N participants whose ICP amounts sum to `total_icp_e8s`, the sum of their individual allocations satisfies:

```
Σ scale(a_i, total_sns, total_icp) ≤ total_sns_e8s
```

with a potential shortfall of up to `N − 1` e8s. The code itself acknowledges this at the end of `create_sns_neuron_recipes`:

```rust
log!(INFO, "... Participants receive a total of {} out of {} (change {});",
    total_sns_tokens_sold_e8s,
    sns_being_offered_e8s,
    sns_being_offered_e8s - total_sns_tokens_sold_e8s   // ← remainder, never distributed
);
```

After `sweep_sns` transfers exactly `total_sns_tokens_sold_e8s` worth of tokens to neuron subaccounts, the Swap canister retains `sns_being_offered_e8s − total_sns_tokens_sold_e8s` SNS tokens in its own ledger account. There is no function in the Swap canister to withdraw or redistribute these leftover tokens. [1](#0-0) [2](#0-1) [3](#0-2) 

---

### Impact Explanation

SNS tokens (up to `N − 1` e8s, where N is the number of swap participants) are permanently locked in the Swap canister's SNS ledger account after every successful swap finalization. The Swap canister exposes no admin or governance function to recover these tokens. They are effectively burned. With large participant counts (e.g., 10,000 participants), up to 9,999 e8s can be lost per swap. Across multiple SNS launches this accumulates into a meaningful ledger conservation violation.

The integration test that asserts `swap_canister_balance_sns_e8s == 0` after finalization only passes because the test uses amounts that divide evenly; it does not cover the general case. [4](#0-3) 

---

### Likelihood Explanation

This is triggered by normal, unprivileged swap participation via `refresh_buyer_tokens`. Any SNS swap with two or more participants whose ICP contributions do not divide the total SNS offering evenly will produce stuck tokens. This is the common case in production. No special attacker capability is required — ordinary participation is sufficient. [5](#0-4) 

---

### Recommendation

After `create_sns_neuron_recipes`, assign the remainder (`sns_being_offered_e8s − total_sns_tokens_sold_e8s`) to one participant (e.g., the largest contributor, analogous to `apportion_approximately_equally`'s approach of distributing the remainder to the last elements), or transfer it to the SNS treasury/governance account during `sweep_sns`. Alternatively, expose a governance-controlled function to sweep residual SNS token balances from the Swap canister to the SNS treasury after finalization. [6](#0-5) 

---

### Proof of Concept

Consider a swap offering `total_sns_e8s = 10` SNS tokens (in e8s) with three participants contributing `3`, `3`, and `4` ICP e8s (total = 10):

- Participant 1: `scale(3, 10, 10) = ⌊30/10⌋ = 3`
- Participant 2: `scale(3, 10, 10) = ⌊30/10⌋ = 3`
- Participant 3: `scale(4, 10, 10) = ⌊40/10⌋ = 4`
- Total distributed: 10 — no loss here.

Now with `total_sns_e8s = 10` and three participants contributing `3`, `3`, `3` ICP e8s (total = 9):

- Participant 1: `scale(3, 10, 9) = ⌊30/9⌋ = 3`
- Participant 2: `scale(3, 10, 9) = ⌊30/9⌋ = 3`
- Participant 3: `scale(3, 10, 9) = ⌊30/9⌋ = 3`
- Total distributed: 9 out of 10 — **1 e8 stuck**.

With N participants each contributing an amount that produces a fractional remainder, up to N−1 e8s are permanently lost per swap. [7](#0-6) [8](#0-7)

### Citations

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

**File:** rs/sns/swap/src/swap.rs (L815-852)
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

**File:** rs/nervous_system/integration_tests/tests/sns_lifecycle.rs (L1336-1342)
```rust
        if swap_finalization_status == SwapFinalizationStatus::Aborted {
            // If the swap fails, the SNS swap does not distribute any tokens.
            assert_eq!(swap_canister_balance_sns_e8s, swap_distribution_sns_e8s);
        } else {
            // In a happy scenario, the SNS swap distributes all the tokens.
            assert_eq!(swap_canister_balance_sns_e8s, 0);
        }
```
