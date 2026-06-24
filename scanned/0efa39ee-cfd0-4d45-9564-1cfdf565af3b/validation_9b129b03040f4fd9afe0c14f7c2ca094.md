### Title
SNS Swap `scale()` Floor Division Causes Permanent SNS Token Lock After Finalization - (File: rs/sns/swap/src/swap.rs)

### Summary
The `scale()` function in the SNS Swap canister uses floor integer division to compute each participant's SNS token allocation during swap finalization. Because every participant's allocation is rounded down, the sum of all allocations is strictly less than the total SNS tokens held by the swap canister. The residual tokens have no recovery path and are permanently locked in the swap canister after `sweep_sns` completes.

### Finding Description
`Swap::scale()` at line 742 of `rs/sns/swap/src/swap.rs` computes each participant's SNS token share as:

```rust
fn scale(amount_icp_e8s: u64, total_sns_e8s: u64, total_icp_e8s: NonZeroU64) -> u64 {
    let r = (amount_icp_e8s as u128)
        .saturating_mul(total_sns_e8s as u128)
        .div(NonZeroU128::from(total_icp_e8s));
    r as u64
}
``` [1](#0-0) 

This is a floor (truncating) integer division. For N participants with ICP contributions `a_1 … a_N` summing to `total_icp_e8s`, the total SNS tokens distributed equals:

```
Σ floor(a_i × total_sns_e8s / total_icp_e8s)  ≤  total_sns_e8s
```

The deficit is at most `N − 1` e8s. These residual tokens remain in the swap canister's SNS ledger account after `sweep_sns` finishes transferring all neuron recipes. [2](#0-1) 

`sweep_sns` iterates over `self.neuron_recipes` and transfers each recipe's `amount_e8s` to the corresponding neuron staking subaccount. There is no subsequent "sweep residual" step and no mechanism to transfer the leftover tokens to the SNS treasury or governance canister. [3](#0-2) 

A secondary rounding step occurs inside `create_sns_neuron_recipes` when the per-participant SNS amount is further split across a neuron basket via `apportion_approximately_equally`. If `scale()` returns a value smaller than `basket_count × transaction_fee_e8s`, individual basket neurons receive amounts below the ledger minimum, causing `sweep_sns` to record them as `TransferResult::AmountTooSmall` (counted as `invalid`), permanently denying those neurons their tokens. [4](#0-3) 

The `create_sns_neuron_recipes` function tracks `total_sns_tokens_sold_e8s` with the stated intent to "check that the amount is correct at the end." If that final check uses strict equality against `sns_being_offered_e8s`, the function will panic on every swap with non-zero rounding loss, producing a deterministic denial-of-service that permanently blocks finalization. [5](#0-4) 

### Impact Explanation
**Ledger conservation bug / potential denial-of-service.** In the best case (no strict equality check), up to `N − 1` e8s of SNS tokens are permanently locked in the swap canister after every finalization, irrecoverable without a canister upgrade. In the worst case (strict equality check at the end of `create_sns_neuron_recipes`), the function panics on every real-world swap, permanently blocking the `finalize` → `create_sns_neuron_recipes` → `sweep_sns` pipeline and freezing all participant funds in the swap canister.

### Likelihood Explanation
This condition is triggered on every SNS swap where `total_icp_e8s` does not evenly divide `total_sns_e8s × a_i` for every participant — which is true in virtually all real swaps. The entry path is fully unprivileged: any user who calls `refresh_buyer_token_e8s` to participate in an open swap contributes to the state that triggers this path at finalization.

### Recommendation
Replace the per-participant `scale()` call with a single call to `apportion_approximately_equally(sns_being_offered_e8s, participant_count)` so that the full token supply is distributed with remainder tokens assigned to specific participants rather than discarded. Alternatively, add a post-`sweep_sns` step that transfers any remaining swap canister SNS balance to the SNS governance treasury account. Remove any strict equality assertion on `total_sns_tokens_sold_e8s` vs `sns_being_offered_e8s` until the distribution logic is made exact.

### Proof of Concept
**Concrete numeric example** (3 participants, 10 SNS tokens offered):

| Participant | ICP contributed | `scale(a_i, 10, 3)` |
|---|---|---|
| Alice | 1 | `floor(1×10/3) = 3` |
| Bob | 1 | `floor(1×10/3) = 3` |
| Carol | 1 | `floor(1×10/3) = 3` |
| **Total** | **3** | **9 (not 10)** |

1 SNS token (1 e8 in e8s terms) remains permanently locked in the swap canister. With a basket of 5 neurons per participant and a transaction fee of 10 000 e8s, each participant's 3 e8s is split as `[1, 1, 1, 0, 0]` by `apportion_approximately_equally`, causing 2 of 5 neurons per participant to hit `AmountTooSmall` and lose their allocation entirely. [6](#0-5) [7](#0-6)

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

**File:** rs/sns/swap/src/swap.rs (L832-834)
```rust
        // Keep track of SNS tokens sold just to check that the amount
        // is correct at the end.
        let mut total_sns_tokens_sold_e8s: u64 = 0;
```

**File:** rs/sns/swap/src/swap.rs (L2165-2298)
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
```
