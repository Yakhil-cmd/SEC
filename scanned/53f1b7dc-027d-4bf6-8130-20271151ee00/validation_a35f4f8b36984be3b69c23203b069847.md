### Title
Unbounded Synchronous Participant Scan in `create_sns_neuron_recipes` Leaves SNS Swap Permanently Stuck with Finalization Lock Set - (`File: rs/sns/swap/src/swap.rs`)

---

### Summary

The SNS Swap canister's `create_sns_neuron_recipes` function iterates over all direct buyers and all Neurons' Fund participants synchronously, with no instruction-limit check. It is called from `finalize_inner` **after** the `finalize_swap_in_progress` lock has already been committed to stable state across a prior `await` boundary. If the synchronous scan exhausts the IC instruction budget and traps, the message-level state rollback does **not** undo the lock, leaving the swap permanently stuck in `Committed` with `finalize_swap_in_progress = true`. All subsequent calls to `finalize_swap` return immediately with an error, and participant ICP/SNS funds cannot be distributed without a canister upgrade.

---

### Finding Description

`finalize` acquires the lock and then calls `finalize_inner`: [1](#0-0) 

Inside `finalize_inner`, the first two `await` points are `sweep_icp` and `settle_neurons_fund_participation`: [2](#0-1) 

Each `await` is a message boundary on the IC. After the first `await` at line 1558, the lock (`finalize_swap_in_progress = true`) is **committed** to stable state and cannot be rolled back by a later trap.

`create_sns_neuron_recipes` is then called **synchronously** at line 1588. It contains two nested loops with no instruction-limit guard:

```rust
// Loop 1: all direct buyers
for (buyer_principal, buyer_state) in self.buyers.iter_mut() {
    // creates `count` recipes per buyer
    match create_sns_neuron_basket_for_direct_participant(...) { ... }
}

// Loop 2: all Neurons' Fund participants × their cf_neurons
for neurons_fund_participant in self.cf_participants.iter_mut() {
    for neurons_fund_neuron in neurons_fund_participant.cf_neurons.iter_mut() {
        // creates `count` recipes per NF neuron
    }
}
``` [3](#0-2) [4](#0-3) 

There is no `is_over_instructions_limit()` call anywhere in this function. The grep confirms zero such guards in the entire swap source: [5](#0-4) 

A second unbounded synchronous scan exists in `claim_swap_neurons` (called at line 1602, after three prior `await`s), which builds a full `claimable_neurons_index` over all `neuron_recipes` before its first `await`: [6](#0-5) 

The code itself acknowledges the lock-stuck risk but only mentions panics, not instruction-limit traps: [7](#0-6) 

---

### Impact Explanation

If `create_sns_neuron_recipes` (or the `claim_swap_neurons` pre-loop) traps due to instruction exhaustion:

1. The IC rolls back only the **current message execution's** state changes.
2. The `finalize_swap_in_progress = true` lock, committed in a **prior** message execution, is **not** rolled back.
3. Every subsequent call to `finalize_swap` hits the lock check at line 1506 and returns immediately with an error.
4. The swap is permanently stuck in `Lifecycle::Committed` with the lock held.
5. Direct participants cannot receive their SNS neurons; ICP already swept to SNS governance cannot be reclaimed. Recovery requires a canister upgrade to clear the lock.

The maximum participant counts are bounded by:
- Direct participants: `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS` (≈100,000 neuron recipes, inferred from the test using `basket_count=33,000` with 3 participants reaching the cap)
- NF participants: `MAX_NEURONS_FUND_PARTICIPANTS = 5,000` [8](#0-7) [9](#0-8) 

At maximum scale, `create_sns_neuron_recipes` must process up to `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS + MAX_SNS_NEURONS_PER_BASKET × MAX_NEURONS_FUND_PARTICIPANTS` recipe-creation operations in a single synchronous message execution. The integration test explicitly documents that these limits are designed to stay under `MAX_NUMBER_OF_NEURONS_CEILING`: [10](#0-9) 

However, the ceiling is a memory bound, not an instruction-count bound. The instruction cost of `create_sns_neuron_basket_for_direct_participant` (hashing, struct allocation, `Vec::extend`) per recipe is non-trivial, and no benchmarks or instruction-limit guards protect the synchronous path.

---

### Likelihood Explanation

- **Entry path**: `finalize_swap` is a public update call on the SNS Swap canister, callable by any principal with no access control.
- **Trigger condition**: A swap with a large number of participants (near `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS` direct + large NF participation) reaches `Committed` and `finalize_swap` is called.
- **Attacker role**: Any unprivileged user who participated in the swap (or any third party) can call `finalize_swap`. No special privilege is required.
- **Realistic scenario**: High-profile SNS launches attract many participants. The participant cap exists to bound memory, not instruction cost. A swap near the cap could trigger the trap.

---

### Recommendation

1. Add an `is_over_instructions_limit()` check inside both loops in `create_sns_neuron_recipes`, persisting partial progress via the existing `has_created_neuron_recipes` idempotency flag, and return a partial `SweepResult` so the caller can retry.
2. Apply the same chunked-processing pattern to the `claim_swap_neurons` pre-loop (lines 1675–1697), splitting it across multiple messages if needed.
3. Add a post-upgrade hook that unconditionally clears `finalize_swap_in_progress` as a safety net (the comment at line 1528 already acknowledges this need but it is not implemented).
4. Add an instruction-count benchmark for `create_sns_neuron_recipes` at maximum participant counts, analogous to the existing memory-bound test in `constraints_dependencies.rs`.

---

### Proof of Concept

1. Launch an SNS swap with `min_participant_icp_e8s` set to the minimum and `neuron_basket_construction_parameters.count` set to a large value (e.g., 10).
2. Fill the swap to near `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS` direct participants and ensure maximum NF participation (`MAX_NEURONS_FUND_PARTICIPANTS = 5,000`).
3. Allow the swap to reach `Lifecycle::Committed`.
4. Call `finalize_swap` from any principal.
5. `sweep_icp` completes → lock is committed to state.
6. `create_sns_neuron_recipes` is called synchronously over ~100,000+ recipe-creation operations → instruction limit trap.
7. Observe: `finalize_swap_in_progress = true` persists; all subsequent `finalize_swap` calls return `"Finalize swap is already in progress"`.
8. Swap is permanently stuck; participant funds cannot be distributed without a canister upgrade. [11](#0-10) [12](#0-11)

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

**File:** rs/sns/swap/src/swap.rs (L1181-1197)
```rust
            let num_direct_participants = self.buyers.len() as u64;
            let num_sns_neurons_per_basket = params
                .neuron_basket_construction_parameters
                .as_ref()
                .expect("neuron_basket_construction_parameters must be specified")
                .count;
            if (num_direct_participants + 1) * num_sns_neurons_per_basket
                > MAX_NEURONS_FOR_DIRECT_PARTICIPANTS
            {
                return Err(format!(
                    "The swap has reached the maximum number of direct participants ({num_direct_participants}) and does \
                     not accept new participants; existing participants may still increase their \
                     ICP participation amount. This constraint ensures that SNS neuron baskets can \
                     be created for all existing participants (SNS neuron basket size: {num_sns_neurons_per_basket}, \
                     MAX_NEURONS_FOR_DIRECT_PARTICIPANTS: {MAX_NEURONS_FOR_DIRECT_PARTICIPANTS}).",
                ));
            }
```

**File:** rs/sns/swap/src/swap.rs (L1500-1534)
```rust
    pub async fn finalize(
        &mut self,
        now_fn: fn(bool) -> u64,
        environment: &mut impl CanisterEnvironment,
    ) -> FinalizeSwapResponse {
        // Acquire the lock or return a FinalizeSwapResponse with an error message.
        if let Err(error_message) = self.lock_finalize_swap() {
            return FinalizeSwapResponse::with_error(error_message);
        }

        // The lock is now acquired and asynchronous calls to finalize are blocked.
        // Perform all subactions.
        let finalize_swap_response = self.finalize_inner(now_fn, environment).await;

        if finalize_swap_response.has_error_message() {
            log!(
                ERROR,
                "The swap did not finalize successfully. \n\
                finalize_swap_response: {finalize_swap_response:?}"
            );
        } else {
            log!(
                INFO,
                "The swap finalized successfully. \n\
                finalize_swap_response: {finalize_swap_response:?}"
            );
        }

        // Release the lock. Note, if there is a panic, the lock will
        // not be released. In that case, the Swap canister will need
        // to be upgraded to release the lock.
        self.unlock_finalize_swap();

        finalize_swap_response
    }
```

**File:** rs/sns/swap/src/swap.rs (L1544-1624)
```rust
    pub async fn finalize_inner(
        &mut self,
        now_fn: fn(bool) -> u64,
        environment: &mut impl CanisterEnvironment,
    ) -> FinalizeSwapResponse {
        let mut finalize_swap_response = FinalizeSwapResponse::default();

        if let Err(e) = self.can_finalize() {
            finalize_swap_response.set_error_message(e);
            return finalize_swap_response;
        }

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
