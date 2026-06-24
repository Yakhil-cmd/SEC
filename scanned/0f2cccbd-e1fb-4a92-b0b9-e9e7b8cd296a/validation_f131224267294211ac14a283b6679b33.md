### Title
Precision Loss in `Swap::scale()` Causes Permanent SNS Token Lockup in Swap Canister - (File: `rs/sns/swap/src/swap.rs`)

---

### Summary

In the SNS Swap canister, each participant's SNS token allocation is computed independently via integer division in `Swap::scale()`. Due to truncation, the sum of all per-participant allocations is strictly less than `sns_token_e8s`. The residual tokens remain permanently locked in the Swap canister with no recovery path.

---

### Finding Description

`Swap::scale()` computes each participant's SNS token share as:

```
amount_sns_e8s = (buyer_icp_e8s * total_sns_e8s) / total_icp_e8s
```

using integer (truncating) division in 128-bit space. [1](#0-0) 

In `create_sns_neuron_recipes()`, this function is called **independently for every direct participant** and **every Neurons' Fund neuron**: [2](#0-1) [3](#0-2) 

Each call independently truncates the division remainder. The code itself tracks the discrepancy in `total_sns_tokens_sold_e8s` and logs it as "change" — but **never redistributes it**: [4](#0-3) 

The "change" (`sns_being_offered_e8s - total_sns_tokens_sold_e8s`) stays in the Swap canister's SNS ledger account. There is no admin function, no recovery path, and no mechanism to send these tokens anywhere after finalization.

---

### Impact Explanation

SNS tokens of real economic value are permanently locked in the Swap canister after every committed swap with more than one participant (unless all per-participant divisions happen to be exact). The maximum loss per swap is bounded by `(N - 1)` e8s where N is the total number of participant slots (direct buyers + Neurons' Fund neurons), but for swaps with hundreds of participants this can be a non-trivial amount. The Swap canister has no upgrade hook or governance action to recover these tokens.

The integration test at line 1341 asserts the swap canister balance reaches zero after finalization, but this assertion is only satisfied when the test values happen to divide evenly — it does not hold in general: [5](#0-4) 

---

### Likelihood Explanation

This triggers in virtually every real-world SNS swap. Any unprivileged user can participate in a swap by calling `refresh_buyer_tokens`. Once the swap commits and `finalize_swap` is called (automatically via heartbeat or manually), `create_sns_neuron_recipes()` runs and the precision loss is guaranteed whenever `(buyer_icp * total_sns) % total_icp != 0` for any participant — which is the common case with arbitrary ICP contribution amounts.

---

### Recommendation

After computing all per-participant allocations, assign the residual tokens (`sns_being_offered_e8s - total_sns_tokens_sold_e8s`) to one participant (e.g., the last one processed, or the SNS treasury/governance canister), rather than leaving them stranded. This mirrors the fix recommended in the external report: compute the last recipient's amount as `total - sum_of_others` rather than as an independent ratio calculation.

---

### Proof of Concept

**Concrete example:**
- `sns_token_e8s = 10` (10 SNS tokens offered)
- Two direct participants: Buyer A with 3 ICP, Buyer B with 7 ICP → `total_icp = 10`

```
scale(3, 10, 10) = (3 * 10) / 10 = 3   ✓
scale(7, 10, 10) = (7 * 10) / 10 = 7   ✓
total_sold = 10  (exact in this case)
```

Now with non-divisible amounts:
- `sns_token_e8s = 10`, Buyer A: 3 ICP, Buyer B: 4 ICP, Buyer C: 3 ICP → `total_icp = 10`

```
scale(3, 10, 10) = 3
scale(4, 10, 10) = 4
scale(3, 10, 10) = 3
total_sold = 10  (exact again)
```

With a more realistic case — `sns_token_e8s = 7`, Buyer A: 3 ICP, Buyer B: 4 ICP → `total_icp = 7`:

```
scale(3, 7, 7) = (3 * 7) / 7 = 3
scale(4, 7, 7) = (4 * 7) / 7 = 4
total_sold = 7  (exact)
```

With `sns_token_e8s = 10`, Buyer A: 3 ICP, Buyer B: 4 ICP → `total_icp = 7`:

```
scale(3, 10, 7) = 30 / 7 = 4   (truncated from 4.28...)
scale(4, 10, 7) = 40 / 7 = 5   (truncated from 5.71...)
total_sold = 9
change = 10 - 9 = 1 SNS token permanently locked
```

The 1 e8 SNS token remains in the Swap canister's ledger account with no recovery mechanism, as confirmed by the log statement at line 985 which only logs the "change" without acting on it. [6](#0-5) [7](#0-6)

### Citations

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

**File:** rs/nervous_system/integration_tests/tests/sns_lifecycle.rs (L1339-1342)
```rust
        } else {
            // In a happy scenario, the SNS swap distributes all the tokens.
            assert_eq!(swap_canister_balance_sns_e8s, 0);
        }
```
