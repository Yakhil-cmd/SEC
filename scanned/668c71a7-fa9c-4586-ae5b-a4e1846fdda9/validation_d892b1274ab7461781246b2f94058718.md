### Title
Integer Division Truncation in `Swap::scale` Permanently Strands SNS Tokens - (File: `rs/sns/swap/src/swap.rs`)

### Summary
The `Swap::scale` function used in `create_sns_neuron_recipes` performs integer (floor) division when computing each buyer's proportional SNS token allocation. The truncated remainder is never redistributed or returned, permanently stranding SNS tokens in the swap canister after every successful swap with non-divisible participation amounts.

### Finding Description
In `rs/sns/swap/src/swap.rs`, the private `scale` function computes each participant's SNS token entitlement as:

```rust
fn scale(amount_icp_e8s: u64, total_sns_e8s: u64, total_icp_e8s: NonZeroU64) -> u64 {
    let r = (amount_icp_e8s as u128)
        .saturating_mul(total_sns_e8s as u128)
        .div(NonZeroU128::from(total_icp_e8s));
    r as u64
}
``` [1](#0-0) 

This is called for every direct participant and every Neurons' Fund neuron inside `create_sns_neuron_recipes`: [2](#0-1) [3](#0-2) 

The integer division truncates the fractional part. The code itself acknowledges the discrepancy in its log statement, calling the difference "change":

```
"... Participants receive a total of {} out of {} (change {});",
total_sns_tokens_sold_e8s,
sns_being_offered_e8s,
sns_being_offered_e8s - total_sns_tokens_sold_e8s
``` [4](#0-3) 

There is no code path that returns this "change" to the SNS project treasury, redistributes it to buyers, or otherwise recovers it. The stranded tokens remain in the swap canister's SNS ledger balance indefinitely.

### Impact Explanation
**High.** For every SNS swap with N participants whose ICP contributions are not exact multiples of `total_icp_e8s / total_sns_e8s`, up to N e8s of SNS tokens are permanently lost — one truncated e8 per participant. With large participant counts (SNS swaps routinely have hundreds to thousands of participants), the aggregate loss can be material. The stranded tokens are real SNS governance tokens that neither buyers receive nor the project recovers. This is a ledger conservation bug: `sns_being_offered_e8s` tokens enter the swap but fewer than `sns_being_offered_e8s` tokens are ever distributed.

**Concrete example:**
- 3 buyers each contribute 1 ICP (= 1e8 e8s); `total_icp_e8s` = 3e8
- `sns_being_offered_e8s` = 10
- Each buyer gets `floor(1e8 × 10 / 3e8)` = `floor(3.33…)` = **3** SNS tokens
- Total distributed = 9; **1 SNS token is permanently stranded**

### Likelihood Explanation
**High.** Integer division truncation occurs whenever `(amount_icp_e8s × total_sns_e8s)` is not exactly divisible by `total_icp_e8s`. Because ICP contributions are arbitrary user-chosen amounts in e8s and `total_sns_e8s` is set by the SNS project, exact divisibility is essentially never guaranteed in practice. This affects every real-world SNS swap.

### Recommendation
After the per-participant loop completes, compute the remainder:

```rust
let remainder_e8s = sns_being_offered_e8s
    .saturating_sub(total_sns_tokens_sold_e8s);
```

Then either:
1. Transfer `remainder_e8s` back to the SNS governance/treasury canister, or
2. Distribute it to the largest contributor (or the last processed participant) to ensure full conservation.

This mirrors the standard fix for the Solidity analog: account for the division remainder explicitly rather than silently discarding it.

### Proof of Concept
The log line at `rs/sns/swap/src/swap.rs:985` already prints the stranded amount on every swap finalization:

```
sns_being_offered_e8s - total_sns_tokens_sold_e8s
``` [5](#0-4) 

To reproduce deterministically: deploy an SNS swap with `sns_token_e8s = 10`, have 3 participants each contribute exactly `total_icp / 3 + 1` e8s (forcing non-divisibility), finalize the swap, and observe that `total_sns_tokens_sold_e8s < sns_being_offered_e8s` while no transfer of the difference is ever made to any account.

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
