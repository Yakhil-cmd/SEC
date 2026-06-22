### Title
No Minimum SNS Token Output Check in SNS Swap `refresh_buyer_token_e8s` — (`rs/sns/swap/src/swap.rs`)

---

### Summary

The SNS Swap canister's `refresh_buyer_token_e8s` function accepts ICP participation with no way for a user to specify a minimum SNS token amount they expect to receive. The actual SNS token allocation is computed at finalization time via a proportional `Swap::scale` calculation over the **final** total ICP pool. A user who commits ICP when `sns_tokens_per_icp` is high can receive a fraction of the tokens they expected if many more participants join before the swap closes — with no on-chain protection mechanism.

---

### Finding Description

When a participant calls `refresh_buyer_token_e8s`, their ICP balance is recorded in `self.buyers`. The `RefreshBuyerTokensRequest` message only carries `buyer` and `confirmation_text` — there is no `min_sns_tokens_expected` field. [1](#0-0) 

The actual SNS token allocation is deferred entirely to `create_sns_neuron_recipes`, called during `finalize_inner`: [2](#0-1) 

Inside `create_sns_neuron_recipes`, each buyer's SNS allocation is computed as:

```
amount_sns_e8s = Swap::scale(
    buyer_state.amount_icp_e8s(),   // Alice's ICP
    sns_being_offered_e8s,          // fixed total SNS pool
    total_participant_icp_e8s,      // ALL ICP at finalization time
)
``` [3](#0-2) 

The `total_participant_icp_e8s` is the sum of every participant's ICP at the moment `finalize` is called — not at the moment Alice committed. The `DerivedState.sns_tokens_per_icp` that Alice observed when she committed is explicitly a `float` and is documented as not suitable for precise financial accounting: [4](#0-3) [5](#0-4) 

**Concrete scenario:**

1. Swap opens with 10,000 SNS tokens. Alice observes `sns_tokens_per_icp = 1000` (10 ICP total so far) and commits 1 ICP, expecting ~1,000 SNS tokens.
2. Before the swap closes, 90 more ICP is committed by other participants (total = 100 ICP).
3. At finalization, Alice receives `1 * 10,000 / 100 = 100 SNS tokens` — a 90% reduction from her expectation.
4. Alice's ICP is already locked; she cannot withdraw it.

There is no `min_sns_tokens_expected` guard anywhere in the participation path.

---

### Impact Explanation

A participant who commits ICP based on the current `sns_tokens_per_icp` rate can receive a fraction of the SNS tokens they expected. Because ICP is locked once `refresh_buyer_token_e8s` succeeds and the swap commits, the user cannot exit. The loss is proportional to how much additional ICP joins after the user commits — in theory unbounded (approaching 100% loss of expected tokens if the pool grows by orders of magnitude). The ICP itself is not lost (it is transferred to SNS governance on commit), but the SNS token return is far below what the user anticipated when they made their economic decision.

---

### Likelihood Explanation

The SNS swap OPEN phase can last up to 14 days. Early participants who commit ICP when participation is low will systematically receive fewer SNS tokens than the rate they observed. This is not a rare edge case — it is the normal operation of any popular SNS launch where participation grows over time. The IC's deterministic message ordering means there is no mempool-based front-running, but the temporal ordering effect is identical: later participants dilute earlier ones with no recourse for the earlier participant. [6](#0-5) 

---

### Recommendation

Add an optional `min_sns_tokens_e8s` field to `RefreshBuyerTokensRequest`. At the time `refresh_buyer_token_e8s` records the participation, compute the **current** expected SNS allocation using the live `sns_tokens_per_icp` rate and reject (or warn) if it falls below the user-supplied minimum. Alternatively, document prominently that the displayed rate is non-binding and that users should only commit ICP they are willing to exchange at any price down to `sns_token_e8s / max_direct_participation_icp_e8s`. [1](#0-0) 

---

### Proof of Concept

1. Deploy an SNS swap with `sns_token_e8s = 10_000 * E8`, `max_direct_participation_icp_e8s = 1000 * E8`, `min_direct_participation_icp_e8s = 10 * E8`.
2. Alice calls `refresh_buyer_token_e8s` when only 10 ICP has been committed. `derived_state().sns_tokens_per_icp ≈ 1000`. Alice commits 1 ICP expecting ~1,000 SNS tokens.
3. 989 more ICP is committed by other participants before the swap closes (total = 1000 ICP, hitting `max_direct_participation_icp_e8s`).
4. Swap commits. `create_sns_neuron_recipes` calls `Swap::scale(1 * E8, 10_000 * E8, 1000 * E8)` for Alice → Alice receives `10 SNS tokens` per ICP, not 1,000.
5. Alice's actual return is 1% of what she expected. No check in `refresh_buyer_token_e8s` could have protected her. [7](#0-6)

### Citations

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L818-820)
```text
  // Current approximate rate SNS tokens per ICP. Note that this should not be used for super
  // precise financial accounting, because this is floating point.
  float sns_tokens_per_icp = 2;
```

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L843-855)
```text
message RefreshBuyerTokensRequest {
  // If not specified, the caller is used.
  string buyer = 1;

  // To accept the swap participation confirmation, a participant should send
  // the confirmation text via refresh_buyer_tokens, matching the text set
  // during SNS initialization.
  optional string confirmation_text = 2;
}
message RefreshBuyerTokensResponse {
  uint64 icp_accepted_participation_e8s = 1;
  uint64 icp_ledger_account_balance_e8s = 2;
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

**File:** rs/sns/swap/src/swap.rs (L1586-1591)
```rust
        // Create the SnsNeuronRecipes based on the contribution of direct and NF participants
        finalize_swap_response
            .set_create_sns_neuron_recipes_result(self.create_sns_neuron_recipes());
        if finalize_swap_response.has_error_message() {
            return finalize_swap_response;
        }
```

**File:** rs/sns/swap/src/swap.rs (L2992-2995)
```rust
        let sns_tokens_per_icp = i2d(tokens_available_for_swap)
            .checked_div(i2d(participant_total_icp_e8s))
            .and_then(|d| d.to_f32())
            .unwrap_or(0.0);
```

**File:** rs/sns/swap/src/types.rs (L320-321)
```rust
    const MIN_SALE_DURATION_SECONDS: u64 = ONE_DAY_SECONDS;
    const MAX_SALE_DURATION_SECONDS: u64 = 14 * ONE_DAY_SECONDS;
```
