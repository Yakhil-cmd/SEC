Audit Report

## Title
Unbounded Synchronous Scan in `create_sns_neuron_recipes` Permanently Locks SNS Swap Finalization After Instruction Exhaustion - (File: rs/sns/swap/src/swap.rs)

## Summary

`create_sns_neuron_recipes` iterates synchronously over all direct buyers and all Neurons' Fund participants with no instruction-limit guard. It is called from `finalize_inner` after two `await` boundaries, meaning the `finalize_swap_in_progress` lock is already committed to stable state in a prior message execution. If the synchronous scan exhausts the IC instruction budget and traps, only the current message's state is rolled back — the lock is not. Every subsequent call to `finalize_swap` returns immediately with "Finalize swap is already in progress," leaving the swap permanently stuck in `Lifecycle::Committed` until a canister upgrade is performed.

## Finding Description

`finalize` acquires the lock at line 1506 and delegates to `finalize_inner`. Inside `finalize_inner`, two inter-canister `await` points occur before `create_sns_neuron_recipes` is reached:

- Line 1558: `self.sweep_icp(...).await` — first message boundary; lock is committed to stable state.
- Line 1565: `self.settle_neurons_fund_participation(...).await` — second message boundary.
- Line 1588: `self.create_sns_neuron_recipes()` — called **synchronously** in the callback message. [1](#0-0) 

`create_sns_neuron_recipes` (line 777) is a plain `fn`, not `async`. It contains two unbounded loops — one over `self.buyers` (line 839) and one over `self.cf_participants` × `cf_neurons` (line 892) — with no call to any instruction-limit check. The grep for `is_over_instructions_limit`, `performance_counter`, or `instruction_counter` returns zero matches in the entire swap source. [2](#0-1) [3](#0-2) [4](#0-3) 

On the IC, instruction-limit exhaustion causes a message-level trap. State changes committed in **prior** messages (across `await` boundaries) are not rolled back. The lock set in the first message therefore persists. The code itself acknowledges this risk for panics at line 1528–1530 but does not implement a post-upgrade hook to clear the lock automatically: [5](#0-4) 

The idempotency flags (`has_created_neuron_recipes`) cannot help here: if the message traps, those flag writes are also rolled back, so the next call would restart from scratch — but the lock prevents any next call from proceeding.

A secondary unbounded synchronous scan exists in `claim_swap_neurons` (lines 1675–1697), which builds a full `claimable_neurons_index` over all `neuron_recipes` before its first `await`, presenting the same risk after three prior `await` boundaries. [6](#0-5) 

The participant-count bounds are confirmed: `MAX_NEURONS_FUND_PARTICIPANTS = 5,000` in NNS governance, and the integration test in `constraints_dependencies.rs` confirms the combined ceiling is `MAX_SNS_NEURONS_PER_BASKET × MAX_NEURONS_FUND_PARTICIPANTS + MAX_NEURONS_FOR_DIRECT_PARTICIPANTS`. [7](#0-6) [8](#0-7) 

## Impact Explanation

If the trap occurs, the SNS swap is permanently stuck in `Lifecycle::Committed` with `finalize_swap_in_progress = true`. ICP already swept to SNS governance (step completed before the trap) cannot be reclaimed by participants; SNS neurons are never distributed. Recovery requires an NNS governance vote to upgrade the SNS Swap canister and clear the lock — a multi-day process. This constitutes a significant application-level DoS of the SNS framework with concrete, lasting harm to swap participants, matching the **High** impact tier: "Significant SNS security impact with concrete user or protocol harm."

## Likelihood Explanation

`finalize_swap` is a public update call with no access control — any principal can invoke it. The trigger condition is a swap near `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS` direct participants combined with large Neurons' Fund participation. High-profile SNS launches routinely attract many participants, and the participant cap is explicitly a memory bound, not an instruction-count bound. No benchmarks or instruction-limit guards protect the synchronous path. The scenario is realistic for any sufficiently popular SNS launch.

## Recommendation

1. Add an `is_over_instructions_limit()` check inside both loops in `create_sns_neuron_recipes`, returning a partial `SweepResult` when the limit is approached. The existing `has_created_neuron_recipes` idempotency flag already supports resumable execution; the caller in `finalize_inner` should treat a partial result as a retryable condition rather than an error.
2. Apply the same chunked-processing pattern to the pre-loop in `claim_swap_neurons` (lines 1675–1697), splitting index construction across messages if needed.
3. Implement a `post_upgrade` hook that unconditionally clears `finalize_swap_in_progress`. The comment at line 1528 acknowledges this need but it is not implemented.
4. Add an instruction-count benchmark for `create_sns_neuron_recipes` at maximum participant counts, analogous to the existing memory-bound test in `constraints_dependencies.rs`.

## Proof of Concept

1. Deploy an SNS swap with `min_participant_icp_e8s` at minimum and `neuron_basket_construction_parameters.count` set to a large value (e.g., 10).
2. Fill the swap to near `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS` direct participants and ensure maximum Neurons' Fund participation (`MAX_NEURONS_FUND_PARTICIPANTS = 5,000`).
3. Allow the swap to reach `Lifecycle::Committed`.
4. Call `finalize_swap` from any principal.
5. `sweep_icp` completes (first `await`) → lock is committed to stable state.
6. `settle_neurons_fund_participation` completes (second `await`).
7. `create_sns_neuron_recipes` is called synchronously over the full participant set → instruction limit trap.
8. Observe: `finalize_swap_in_progress = true` persists; all subsequent `finalize_swap` calls return `"Finalize swap is already in progress"`.
9. Swap is permanently stuck; participant SNS neurons are never distributed; recovery requires a canister upgrade via NNS governance vote.

A deterministic integration test can reproduce this using PocketIC with a mocked instruction counter that forces a trap mid-loop, then asserting that `finalize_swap_in_progress` remains `true` and subsequent `finalize_swap` calls return the lock-held error.

### Citations

**File:** rs/sns/swap/src/swap.rs (L777-784)
```rust
    pub fn create_sns_neuron_recipes(&mut self) -> SweepResult {
        let Some(params) = self.params.as_ref() else {
            log!(
                ERROR,
                "Halting create_sns_neuron_recipes(). Params is missing",
            );
            return SweepResult::new_with_global_failures(1);
        };
```

**File:** rs/sns/swap/src/swap.rs (L839-883)
```rust
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

**File:** rs/sns/swap/src/swap.rs (L892-975)
```rust
        for neurons_fund_participant in self.cf_participants.iter_mut() {
            let controller = neurons_fund_participant.try_get_controller();

            for neurons_fund_neuron in neurons_fund_participant.cf_neurons.iter_mut() {
                // Create a closure to ensure `global_neurons_fund_memo` is incremented in all cases
                let hotkeys = neurons_fund_neuron.hotkeys.clone().unwrap_or_default();
                let process_neurons_fund_neuron = || {
                    let controller = match controller.clone() {
                        Ok(nns_neuron_controller_principal) => nns_neuron_controller_principal,
                        Err(e) => {
                            log!(
                                ERROR,
                                "Error getting the controller for {neurons_fund_neuron:?} principal: {e}"
                            );
                            sweep_result.invalid +=
                                neuron_basket_construction_parameters.count as u32;
                            return;
                        }
                    };

                    // The case that on a previous attempt at creating this neuron recipe, it was
                    // successfully created and recorded. Count the number of neuron recipes that
                    // would have been created.
                    if neurons_fund_neuron.has_created_neuron_recipes == Some(true) {
                        sweep_result.skipped += neuron_basket_construction_parameters.count as u32;
                        return;
                    }

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
                            neurons_fund_neuron.has_created_neuron_recipes = Some(true);
                        }
                        Err(error_message) => {
                            log!(
                                ERROR,
                                "Error creating a neuron basked for identity {}. Reason: {}",
                                controller,
                                error_message
                            );
                            sweep_result.failure +=
                                neuron_basket_construction_parameters.count as u32;
                        }
                    };
                };

                // Call the closure
                process_neurons_fund_neuron();

                // Increment the memo by the number neurons in a neuron basket. This means that
                // previous idempotent calls should increment global_neurons_fund_memo and handle overflow
                match global_neurons_fund_memo
                    .checked_add(neuron_basket_construction_parameters.count)
                {
                    Some(new_value) => {
                        global_neurons_fund_memo = new_value;
                    }
                    None => {
                        sweep_result.global_failures += 1;
                        // This will exit the entire function, ending all loops, but persist the data that has already been processed
                        return sweep_result;
                    }
                }
            }
        }
```

**File:** rs/sns/swap/src/swap.rs (L1528-1531)
```rust
        // Release the lock. Note, if there is a panic, the lock will
        // not be released. In that case, the Swap canister will need
        // to be upgraded to release the lock.
        self.unlock_finalize_swap();
```

**File:** rs/sns/swap/src/swap.rs (L1556-1591)
```rust
        // Transfer the ICP tokens from the Swap canister.
        finalize_swap_response
            .set_sweep_icp_result(self.sweep_icp(now_fn, environment.icp_ledger()).await);
        if finalize_swap_response.has_error_message() {
            return finalize_swap_response;
        }

        // Settle the Neurons' Fund participation in the token swap.
        finalize_swap_response.set_settle_neurons_fund_participation_result(
            self.settle_neurons_fund_participation(environment.nns_governance_mut())
                .await,
        );
        if finalize_swap_response.has_error_message() {
            return finalize_swap_response;
        }

        if self.should_restore_dapp_control() {
            // Restore controllers of dapp canisters to their original
            // owners (i.e. self.init.fallback_controller_principal_ids).
            finalize_swap_response.set_set_dapp_controllers_result(
                self.restore_dapp_controllers_for_finalize(environment.sns_root_mut())
                    .await,
            );

            // In the case of returning control of the dapp(s) to the fallback
            // controllers, finalize() need not do any more work, so always return
            // and end execution.
            return finalize_swap_response;
        }

        // Create the SnsNeuronRecipes based on the contribution of direct and NF participants
        finalize_swap_response
            .set_create_sns_neuron_recipes_result(self.create_sns_neuron_recipes());
        if finalize_swap_response.has_error_message() {
            return finalize_swap_response;
        }
```

**File:** rs/sns/swap/src/swap.rs (L1675-1697)
```rust
        for recipe in &mut self.neuron_recipes {
            // Here we convert the SnsNeuronRecipe (a Swap concept) to an SnsNeuronRecipe (an SNS
            // Governance concept).
            match recipe.to_neuron_recipe(nns_governance, sns_transaction_fee_e8s) {
                Ok(neuron_recipe) => {
                    let neuron_id = neuron_recipe.neuron_id.clone().expect(
                        "NeuronRecipe.neuron_id is always set by \
                        SnsNeuronRecipe::to_neuron_recipe",
                    );
                    claimable_neurons_index.insert(neuron_id, recipe);
                    neuron_recipes.push(neuron_recipe);
                }
                Err((error_type, error_message)) => {
                    log!(ERROR, "Error creating neuron recipe: {:?}", error_message);
                    match error_type {
                        // In the case of a bug due to programmer error, increment the invalid field.
                        ConversionError::Invalid => sweep_result.invalid += 1,
                        // If we've already processed ths neuron, increment the `skip` field.
                        ConversionError::AlreadyProcessed => sweep_result.skipped += 1,
                    }
                }
            }
        }
```

**File:** rs/nns/governance/src/governance.rs (L280-288)
```rust
/// Maximum allowed number of Neurons' Fund participants that may participate in an SNS swap. Given
/// the maximum number of SNS neurons per swap participant (a.k.a. neuron basket count), this
/// constant can be used to obtain an upper bound for the number of SNS neurons created for the
/// Neurons' Fund participants. See also `MAX_SNS_NEURONS_PER_BASKET`. In addition, this constant
/// also affects the upperbound of instructions needed to draw/refund maturity from/to the Neurons'
/// Fund, so before increasing this constant, the impact on the instructions used by
/// `CreateServiceNervousSystem` proposal execution also needs to be evaluated (currently, each
/// neuron takes ~120K instructions to draw/refund maturity, so the total is ~600M).
pub const MAX_NEURONS_FUND_PARTICIPANTS: u64 = 5_000;
```

**File:** rs/nervous_system/integration_tests/tests/constraints_dependencies.rs (L35-54)
```rust
fn test_max_number_of_sns_neurons_adds_up() {
    const RECOMMENDATION: &str = "If you are adjusting any of these limits, please consider the \
        risks associated with the *order* in which the affected canisters could be *upgraded*. \
        If some of these limits are being decreased, first release NNS Governance and SNS-W, \
        then publish SNS Governance. If some of these limits are being INCREASED, first publish \
        SNS Governance, then wait until all potentially affected SNSes are upgraded, and only then \
        upgrade NNS Governance and SNS-W.";
    assert!(
        NervousSystemParameters::MAX_NUMBER_OF_NEURONS_CEILING
            >= MAX_SNS_NEURONS_PER_BASKET * MAX_NEURONS_FUND_PARTICIPANTS
                + MAX_NEURONS_FOR_DIRECT_PARTICIPANTS
                + MAX_DEVELOPER_DISTRIBUTION_COUNT as u64,
        "MAX_NUMBER_OF_NEURONS_CEILING ({}) must be >= \
         MAX_SNS_NEURONS_PER_BASKET ({MAX_SNS_NEURONS_PER_BASKET}) * \
         MAX_NEURONS_FUND_PARTICIPANTS ({MAX_NEURONS_FUND_PARTICIPANTS}) \
         + MAX_NEURONS_FOR_DIRECT_PARTICIPANTS ({MAX_NEURONS_FOR_DIRECT_PARTICIPANTS}) \
         + MAX_DEVELOPER_DISTRIBUTION_COUNT ({MAX_DEVELOPER_DISTRIBUTION_COUNT}).\n\
         {RECOMMENDATION}",
        NervousSystemParameters::MAX_NUMBER_OF_NEURONS_CEILING
    );
```
