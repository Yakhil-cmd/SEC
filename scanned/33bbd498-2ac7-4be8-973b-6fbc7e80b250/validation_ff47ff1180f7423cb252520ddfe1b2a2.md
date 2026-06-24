### Title
Precision Loss in SNS Swap `scale` Function Permanently Strands SNS Tokens in Swap Canister - (File: rs/sns/swap/src/swap.rs)

### Summary
The `Swap::scale` function in the SNS Swap canister uses floor integer division to compute each participant's SNS token allocation. Because every individual allocation is independently floored, the sum of all allocations is strictly less than `sns_being_offered_e8s` whenever `total_icp_e8s` does not evenly divide `amount_i * total_sns_e8s` for all participants. The residual "change" tokens remain in the swap canister's SNS ledger account permanently, with no on-chain recovery path.

### Finding Description

`create_sns_neuron_recipes` in `rs/sns/swap/src/swap.rs` iterates over all direct buyers and Neurons' Fund participants, calling `Swap::scale` for each:

```rust
fn scale(amount_icp_e8s: u64, total_sns_e8s: u64, total_icp_e8s: NonZeroU64) -> u64 {
    let r = (amount_icp_e8s as u128)
        .saturating_mul(total_sns_e8s as u128)
        .div(NonZeroU128::from(total_icp_e8s));
    r as u64
}
``` [1](#0-0) 

This computes `⌊(amount_i × total_sns) / total_icp⌋`. For N participants whose ICP contributions sum to `total_icp`, the mathematical identity gives:

```
Σ ⌊(amount_i × total_sns) / total_icp⌋  ≤  total_sns
```

The deficit can be as large as `N − 1` e8s. The code itself acknowledges this residual in a log statement:

```rust
log!(
    INFO,
    "... Participants receive a total of {} out of {} (change {});",
    total_sns_tokens_sold_e8s,
    sns_being_offered_e8s,
    sns_being_offered_e8s - total_sns_tokens_sold_e8s   // residual never recovered
);
``` [2](#0-1) 

After `sweep_sns` distributes exactly the amounts recorded in `neuron_recipes`, the swap canister's SNS ledger account retains the residual balance. No subsequent step in `finalize_inner` transfers this balance back to the SNS treasury or governance canister: [3](#0-2) 

The swap canister exposes no public method to reclaim stranded SNS tokens after finalization.

### Impact Explanation

SNS tokens equal to `sns_being_offered_e8s − Σ scale(amount_i, …)` are permanently locked in the swap canister's SNS ledger subaccount after every committed swap. For a swap with 1 000 direct participants the maximum stranded amount is 999 e8s; for 10 000 participants it is 9 999 e8s. These tokens are removed from effective circulation without being burned, creating a permanent discrepancy between the SNS ledger's total supply and the sum of all reachable balances. Recovery requires an NNS-approved canister upgrade of the swap canister.

**Vulnerability class:** Ledger conservation bug (token accounting).

### Likelihood Explanation

The condition `total_icp_e8s | (amount_i × total_sns_e8s)` for every participant simultaneously is almost never satisfied in practice. Any real swap with two or more participants whose ICP contributions are not exact multiples of `total_icp / total_sns` will exhibit the residual. Because every SNS swap goes through this code path on finalization, the bug is triggered on every committed swap.

The entry path is fully unprivileged: any principal can call `refresh_buyer_tokens` on the swap canister to become a participant, and the swap canister's `finalize` endpoint is callable by anyone once the swap is committed. [4](#0-3) 

### Recommendation

Replace the per-participant floor-division approach with a residual-aware distribution. One correct pattern is to compute each participant's allocation with `scale` as today, then assign the entire remaining residual (`sns_being_offered_e8s − total_sns_tokens_sold_e8s`) to the last successfully processed participant, or transfer it to the SNS governance treasury account as part of `finalize_inner`. The existing `apportion_approximately_equally` helper already implements a correct remainder-distributing algorithm and could be reused here. [5](#0-4) 

### Proof of Concept

**Concrete numeric example** (analogous to the report's `openingPrice = 9220` scenario):

```
sns_being_offered_e8s  = 300
total_participant_icp  = 9220   (NonZeroU64)

Participant A: amount_icp = 3000
  scale(3000, 300, 9220) = ⌊(3000 × 300) / 9220⌋ = ⌊900000 / 9220⌋ = 97

Participant B: amount_icp = 6220
  scale(6220, 300, 9220) = ⌊(6220 × 300) / 9220⌋ = ⌊1866000 / 9220⌋ = 202

total_sns_tokens_sold_e8s = 97 + 202 = 299
sns_being_offered_e8s     = 300
change (stranded)         = 1
```

The 1 e8s residual remains in the swap canister's SNS ledger account indefinitely. With 1 000 participants constructed to each produce a fractional remainder, the stranded amount reaches 999 e8s per swap. [6](#0-5) [7](#0-6)

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

**File:** rs/sns/swap/src/swap.rs (L815-868)
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
```

**File:** rs/sns/swap/src/swap.rs (L920-941)
```rust
                    let amount_sns_e8s = Swap::scale(
                        neurons_fund_neuron.amount_icp_e8s,
                        sns_being_offered_e8s,
                        total_participant_icp_e8s,
                    );

                    match create_sns_neuron_basket_for_neurons_fund_participant(
                        &controller,
                        hotkeys.principals,
                        neurons_fund_neuron.nns_neuron_id,
                        amount_sns_e8s,
                        neuron_basket_construction_parameters,
                        global_neurons_fund_memo,
                        nns_governance_canister_id.get(),
                    ) {
                        Ok(cf_participants_sns_neuron_recipes) => {
                            sweep_result.success +=
                                neuron_basket_construction_parameters.count as u32;
                            self.neuron_recipes
                                .extend(cf_participants_sns_neuron_recipes);
                            total_sns_tokens_sold_e8s =
                                total_sns_tokens_sold_e8s.saturating_add(amount_sns_e8s);
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

**File:** rs/sns/swap/src/swap.rs (L1586-1623)
```rust
        // Create the SnsNeuronRecipes based on the contribution of direct and NF participants
        finalize_swap_response
            .set_create_sns_neuron_recipes_result(self.create_sns_neuron_recipes());
        if finalize_swap_response.has_error_message() {
            return finalize_swap_response;
        }

        // Transfer the SNS tokens from the Swap canister.
        finalize_swap_response
            .set_sweep_sns_result(self.sweep_sns(now_fn, environment.sns_ledger()).await);
        if finalize_swap_response.has_error_message() {
            return finalize_swap_response;
        }

        // Once SNS tokens have been distributed to the correct accounts, claim
        // them as neurons on behalf of the Swap participants.
        finalize_swap_response.set_claim_neuron_result(
            self.claim_swap_neurons(environment.sns_governance_mut())
                .await,
        );
        if finalize_swap_response.has_error_message() {
            return finalize_swap_response;
        }

        finalize_swap_response.set_set_mode_call_result(
            Self::set_sns_governance_to_normal_mode(environment.sns_governance_mut()).await,
        );

        // The following step is non-critical, so we'll do it after we set
        // governance to normal mode, but only if there were no errors.
        if !finalize_swap_response.has_error_message() {
            finalize_swap_response.set_set_dapp_controllers_result(
                self.take_sole_control_of_dapp_controllers_for_finalize(environment.sns_root_mut())
                    .await,
            );
        }

        finalize_swap_response
```
