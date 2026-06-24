### Title
SNS Swap Canister Permanently Strands SNS Tokens Due to Per-Participant Floor Division Rounding in `create_sns_neuron_recipes` - (File: rs/sns/swap/src/swap.rs)

### Summary

The SNS swap canister allocates SNS tokens to each participant using a per-participant floor division (`Swap::scale()`). Because integer floor division is applied independently for each buyer, the cumulative rounding loss causes the sum of all allocated SNS tokens to be strictly less than the total SNS tokens loaded into the swap canister. The "change" (leftover tokens) is explicitly logged but is never returned to the SNS treasury — there is no step in `finalize_inner()` to recover these tokens. They are permanently stranded in the swap canister.

### Finding Description

In `create_sns_neuron_recipes()`, for every direct participant and every Neurons' Fund neuron, the SNS token allocation is computed by `Swap::scale()`:

```rust
fn scale(amount_icp_e8s: u64, total_sns_e8s: u64, total_icp_e8s: NonZeroU64) -> u64 {
    let r = (amount_icp_e8s as u128)
        .saturating_mul(total_sns_e8s as u128)
        .div(NonZeroU128::from(total_icp_e8s));
    r as u64
}
``` [1](#0-0) 

This computes `⌊buyer_icp × total_sns / total_icp⌋` — a floor division applied once per participant. For N participants, the mathematical identity

```
Σ ⌊buyer_icp_i × total_sns / total_icp⌋  ≤  total_sns
```

guarantees that the sum of all allocations is at most `total_sns_e8s`, with the deficit being up to `N − 1` e8s. The code itself acknowledges this "change":

```rust
log!(INFO,
    "SNS Neuron Recipes Created; ... Participants receive a total of {} out of {} (change {});",
    total_sns_tokens_sold_e8s,
    sns_being_offered_e8s,
    sns_being_offered_e8s - total_sns_tokens_sold_e8s   // ← leftover, never returned
);
``` [2](#0-1) 

After `create_sns_neuron_recipes()` runs, `finalize_inner()` proceeds to `sweep_sns()` (which transfers only the amounts recorded in the neuron recipes), then claims neurons, sets governance mode, and returns. There is no step that transfers the residual SNS balance back to the SNS treasury. [3](#0-2) 

The `sweep_sns()` function iterates over `self.neuron_recipes` and transfers exactly the `amount_e8s` stored in each recipe — it has no awareness of the total SNS balance held by the canister and makes no attempt to sweep the remainder. [4](#0-3) 

### Impact Explanation

After a committed SNS swap finalizes, up to `N − 1` e8s of SNS tokens (where N is the total number of participant neurons across direct buyers and Neurons' Fund neurons) remain in the swap canister's default account on the SNS ledger with no recovery path. The swap canister has no `withdraw_remaining_sns_tokens` endpoint, no post-finalization cleanup step, and no governance proposal path to reclaim these tokens. They are permanently removed from effective circulation. The integration test that asserts `swap_canister_balance_sns_e8s == 0` after a successful swap [5](#0-4) 

only passes when the test's specific participation amounts happen to divide `sns_token_e8s` evenly, leaving the rounding case untested.

### Likelihood Explanation

This occurs in every SNS swap where `total_sns_e8s` is not perfectly divisible by `total_icp_e8s` across all participants — the common case in production. Any swap with two or more participants whose ICP contributions are not exact multiples of `total_sns_e8s / total_icp_e8s` will strand tokens. The entry path is fully unprivileged: any user calling `refresh_buyer_tokens` to participate in an open SNS swap contributes to the participant set that triggers this rounding at finalization. [6](#0-5) 

### Recommendation

After `sweep_sns()` completes successfully, add a step in `finalize_inner()` that queries the swap canister's own SNS ledger balance and, if non-zero, transfers the residual amount to the SNS governance treasury subaccount (the same destination used for the initial SNS token distribution). This mirrors the correct fix described in the external report: accumulate the exact distributed total during finalization and use that as the authoritative figure rather than recomputing it from a clearing price.

### Proof of Concept

Concrete example:
- `sns_token_e8s = 10`
- 3 direct buyers each contributing 3 ICP e8s (`total_icp = 9`)

Each buyer receives `scale(3, 10, 9) = ⌊3 × 10 / 9⌋ = ⌊3.33⌋ = 3` SNS e8s.  
Total distributed: `3 × 3 = 9` SNS e8s.  
Stranded in swap canister: `10 − 9 = 1` SNS e8s — permanently unrecoverable.

The `apportion_approximately_equally` function used to split each buyer's allocation across their neuron basket does conserve its input total correctly [7](#0-6) 

so the rounding loss occurs solely at the `scale()` layer, not at the basket-splitting layer. The root cause is structurally identical to the external report: a single aggregate value (`total_sns_e8s`) is distributed via N independent floor-division operations, and the sum of the N rounded-down results is less than the original aggregate, with no mechanism to reconcile the difference.

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

**File:** rs/sns/swap/src/swap.rs (L838-852)
```rust
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

**File:** rs/sns/swap/src/swap.rs (L1586-1624)
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
    }
```

**File:** rs/sns/swap/src/swap.rs (L2165-2300)
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

        for recipe in self.neuron_recipes.iter_mut() {
            let neuron_memo = match recipe.neuron_attributes.as_ref() {
                Some(neuron_attributes) => neuron_attributes.memo,
                // SnsNeuronRecipe.neuron_attributes should always be present as it is set in `commit`.
                // In the case of a bug due to programmer error, increment the invalid field.
                // This will require a manual intervention via an upgrade to correct
                None => {
                    log!(
                        ERROR,
                        "Missing neuron attributes information for neuron recipe {:?}",
                        recipe
                    );
                    sweep_result.invalid += 1;
                    continue;
                }
            };

            let dst_subaccount = match &recipe.investor {
                Some(Investor::Direct(DirectInvestment { buyer_principal })) => {
                    match string_to_principal(buyer_principal) {
                        Some(p) => compute_neuron_staking_subaccount_bytes(p, neuron_memo),
                        // principal_str should always be parseable as a PrincipalId as that is enforced
                        // in `refresh_buyer_tokens`. In the case of a bug due to programmer error, increment
                        // the invalid field. This will require a manual intervention via an upgrade to correct
                        None => {
                            sweep_result.invalid += 1;
                            continue;
                        }
                    }
                }
                Some(Investor::CommunityFund(_)) => {
                    compute_neuron_staking_subaccount_bytes(nns_governance.into(), neuron_memo)
                }
                // SnsNeuronRecipe.investor should always be present as it is set in `commit`.
                // In the case of a bug due to programmer error, increment the invalid field.
                // This will require a manual intervention via an upgrade to correct
                None => {
                    log!(
                        ERROR,
                        "Missing investor information for neuron recipe {:?}",
                        recipe,
                    );
                    sweep_result.invalid += 1;
                    continue;
                }
            };
            let dst = Account {
                owner: sns_governance.get().0,
                subaccount: Some(dst_subaccount),
            };

            let sns_transferable_amount = match recipe.sns.as_mut() {
                Some(transferable_amount) => transferable_amount,
                // SnsNeuronRecipe.sns should always be present as it is set in `commit`.
                // In the case of a bug due to programmer error, increment the invalid field.
                // This will require a manual intervention via an upgrade to correct
                None => {
                    log!(
                        ERROR,
                        "Missing transfer information for neuron recipe {:?}",
                        recipe,
                    );
                    sweep_result.invalid += 1;
                    continue;
                }
            };

            let result = sns_transferable_amount
                .transfer_helper(
                    now_fn,
                    sns_transaction_fee_tokens,
                    /* src_subaccount= */ None,
                    &dst,
                    sns_ledger,
                )
                .await;
            match result {
                // AmountToSmall should never happen as the sns token amount is checked in
                // `commit`. In the case of a bug due to programmer error,
                // increment the invalid field. This will require a manual intervention
                // via an upgrade to correct
                TransferResult::AmountTooSmall => {
                    sweep_result.invalid += 1;
                }
                TransferResult::AlreadyStarted => {
                    sweep_result.skipped += 1;
                }
                TransferResult::Success(_) => {
                    let fee_e8s = sns_transaction_fee_tokens.get_e8s();
                    sns_transferable_amount.transfer_fee_paid_e8s = Some(fee_e8s);
                    sns_transferable_amount.amount_transferred_e8s =
                        Some(sns_transferable_amount.amount_e8s - fee_e8s);

                    sweep_result.success += 1;
                }
                TransferResult::Failure(_) => {
                    sweep_result.failure += 1;
                }
            }
        }

        sweep_result
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
