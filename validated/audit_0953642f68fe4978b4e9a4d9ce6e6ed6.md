### Title
Unbounded Synchronous Loop in `create_sns_neuron_recipes` Can Permanently Lock `finalize_swap` - (File: rs/sns/swap/src/swap.rs)

### Summary
`Swap::create_sns_neuron_recipes` iterates over all buyers and all Neurons' Fund participants in a single synchronous (non-async) execution without any batch limit. When called from `finalize_inner` — which is already past multiple `await` points — exceeding the IC instruction limit traps the message and rolls back only that message's state changes, while the `finalize_swap_in_progress` lock (committed in a prior message) remains permanently set. This permanently prevents the swap from being finalized.

### Finding Description

`create_sns_neuron_recipes` is a fully synchronous function that performs two nested unbounded loops:

```rust
// rs/sns/swap/src/swap.rs lines 839–883
for (buyer_principal, buyer_state) in self.buyers.iter_mut() {
    ...
    match create_sns_neuron_basket_for_direct_participant(...) {
        Ok(direct_participant_sns_neuron_recipes) => {
            self.neuron_recipes.extend(direct_participant_sns_neuron_recipes);
            ...
        }
    }
}

// lines 892–975
for neurons_fund_participant in self.cf_participants.iter_mut() {
    for neurons_fund_neuron in neurons_fund_participant.cf_neurons.iter_mut() {
        ...
    }
}
``` [1](#0-0) [2](#0-1) 

This function is called from `finalize_inner` **after** two prior `await` points:

```rust
// rs/sns/swap/src/swap.rs lines 1557–1591
finalize_swap_response
    .set_sweep_icp_result(self.sweep_icp(now_fn, environment.icp_ledger()).await);  // await #1
...
finalize_swap_response.set_settle_neurons_fund_participation_result(
    self.settle_neurons_fund_participation(environment.nns_governance_mut()).await,  // await #2
);
...
finalize_swap_response
    .set_create_sns_neuron_recipes_result(self.create_sns_neuron_recipes());  // SYNC, no await
``` [3](#0-2) 

The `finalize_swap_in_progress` lock is acquired before the first `await` and is committed to replicated state at that point:

```rust
// rs/sns/swap/src/swap.rs lines 1505–1512
if let Err(error_message) = self.lock_finalize_swap() {
    return FinalizeSwapResponse::with_error(error_message);
}
let finalize_swap_response = self.finalize_inner(now_fn, environment).await;
``` [4](#0-3) 

The code itself acknowledges this risk:

```rust
// Release the lock. Note, if there is a panic, the lock will
// not be released. In that case, the Swap canister will need
// to be upgraded to release the lock.
self.unlock_finalize_swap();
``` [5](#0-4) 

The total work in `create_sns_neuron_recipes` scales as `(|buyers| + |cf_neurons|) × neuron_basket_construction_parameters.count`. The `count` field is a `u64` with no enforced upper bound in the swap parameters proto:

```proto
// rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto lines 541–548
message NeuronBasketConstructionParameters {
  uint64 count = 1;
  uint64 dissolve_delay_interval_seconds = 2;
}
``` [6](#0-5) 

While `min_participant_icp_e8s` has a lower bound (`MIN_PARTICIPANT_ICP_LOWER_BOUND_E8S`) to prevent memory overflow, this bound is not calibrated against the IC instruction limit for `create_sns_neuron_recipes`. A swap with `max_direct_participation_icp_e8s / min_participant_icp_e8s` buyers (e.g., tens of thousands) combined with a large `count` (e.g., 7–10 neurons per basket) can produce hundreds of thousands of recipe-creation operations in a single synchronous message, exhausting the ~5 billion instruction limit. [7](#0-6) 

### Impact Explanation

**Impact: High**

If `create_sns_neuron_recipes` traps due to instruction limit exhaustion:
1. The IC rolls back all state changes from that message execution.
2. The `finalize_swap_in_progress = true` lock — committed in a prior message — is **not** rolled back and remains permanently set.
3. All subsequent calls to `finalize_swap` immediately return an error because `lock_finalize_swap` sees the lock is held.
4. The swap is permanently stuck: ICP already swept (step 1 completed), SNS tokens never distributed, neurons never created.
5. Recovery requires a canister upgrade to manually clear the lock, which requires NNS governance action. [8](#0-7) 

### Likelihood Explanation

**Likelihood: Low**

Triggering this requires a swap configured with a large number of participants and/or a large `neuron_basket_construction_parameters.count`. This is not a common configuration but is entirely valid per the protocol. A popular SNS launch with many small participants (each contributing the minimum) combined with a basket count of 7–10 neurons could realistically reach the threshold. No privileged access is required — `finalize_swap` is a public update endpoint callable by any principal. [9](#0-8) 

### Recommendation

Apply the same batching pattern already used by `purge_old_tickets` and `groom_some_neurons`: process a bounded number of buyers per call and persist a cursor (`next_buyer_to_process`) in swap state so that successive calls to `finalize_swap` (or a dedicated periodic task) continue from where the previous call left off. Alternatively, enforce a strict upper bound on `(max_direct_participation_icp_e8s / min_participant_icp_e8s) × neuron_basket_construction_parameters.count` during swap initialization validation to guarantee the total work fits within the instruction limit. [10](#0-9) [11](#0-10) 

### Proof of Concept

1. Deploy an SNS swap with:
   - `min_participant_icp_e8s` = `MIN_PARTICIPANT_ICP_LOWER_BOUND_E8S` (minimum allowed)
   - `max_direct_participation_icp_e8s` = `MAX_DIRECT_ICP_CONTRIBUTION_TO_SWAP` (maximum allowed)
   - `neuron_basket_construction_parameters.count` = 7 (or higher)
2. Fill the swap to capacity with `max_direct_participation_icp_e8s / min_participant_icp_e8s` distinct buyer principals, each contributing the minimum.
3. Allow the swap to reach `Lifecycle::Committed`.
4. Call `finalize_swap({})` from any principal.
5. `sweep_icp` completes (ICP transferred), committing `finalize_swap_in_progress = true` to replicated state.
6. `create_sns_neuron_recipes` is invoked synchronously with all buyers; the instruction limit is exceeded; the message traps.
7. Observe: `finalize_swap_in_progress` remains `true`; all subsequent `finalize_swap` calls return immediately with a lock-held error; SNS tokens are permanently stranded. [12](#0-11) [13](#0-12)

### Citations

**File:** rs/sns/swap/src/swap.rs (L777-810)
```rust
    pub fn create_sns_neuron_recipes(&mut self) -> SweepResult {
        let Some(params) = self.params.as_ref() else {
            log!(
                ERROR,
                "Halting create_sns_neuron_recipes(). Params is missing",
            );
            return SweepResult::new_with_global_failures(1);
        };

        let Some(neuron_basket_construction_parameters) =
            params.neuron_basket_construction_parameters.as_ref()
        else {
            log!(
                ERROR,
                "Halting create_sns_neuron_recipes(). Neuron_basket_construction_parameters is missing",
            );
            return SweepResult::new_with_global_failures(1);
        };

        let init = match self.init_and_validate() {
            Ok(init) => init,
            Err(error_message) => {
                log!(
                    ERROR,
                    "Halting create_sns_neuron_recipes(). Init is missing or corrupted: {:?}",
                    error_message
                );
                return SweepResult::new_with_global_failures(1);
            }
        };
        // The following methods are safe to call since we validated Init in the above block
        let nns_governance_canister_id = init.nns_governance_or_panic();

        let mut sweep_result = SweepResult::default();
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

**File:** rs/sns/swap/src/swap.rs (L1454-1475)
```rust
    /// Releases the lock on `finalize_swap`.
    fn unlock_finalize_swap(&mut self) {
        match self.is_finalize_swap_locked() {
            true => self.finalize_swap_in_progress = Some(false),
            false => {
                log!(
                    ERROR,
                    "Unexpected condition when unlocking finalize_swap_in_progress. \
                    The lock was not held: {:?}.",
                    self.finalize_swap_in_progress
                );
            }
        }
    }

    /// Checks the internal state of `finalize_swap_in_progress` lock.
    pub fn is_finalize_swap_locked(&self) -> bool {
        match self.finalize_swap_in_progress {
            Some(true) => true,
            None | Some(false) => false,
        }
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

**File:** rs/sns/swap/src/swap.rs (L1557-1591)
```rust
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

**File:** rs/sns/swap/src/swap.rs (L2609-2667)
```rust
    pub fn try_purge_old_tickets(
        &mut self,
        now_nanoseconds: impl Fn() -> u64,
        /* amount of tickets after which purge_old_tickets is executed */
        number_of_tickets_threshold: u64,
        /* minimum age of a ticket to be purged */
        max_age_in_nanoseconds: u64,
        /* max number of inspect in a single call */
        max_number_to_inspect: u64,
    ) -> Option<bool> {
        const INTERVAL_NANOSECONDS: u64 = 60 * 10 * 1_000_000_000; // 10 minutes

        if self.lifecycle() != Lifecycle::Open {
            return None;
        }

        // Do not run purge_old_tickets if the number of tickets is less than or equal
        // to the threshold. This should save cycles.
        if memory::OPEN_TICKETS_MEMORY.with(|ts| ts.borrow().len()) < number_of_tickets_threshold {
            return None;
        }

        let purge_old_tickets_last_completion_timestamp_nanoseconds = self
            .purge_old_tickets_last_completion_timestamp_nanoseconds
            .unwrap_or(0);

        let purge_old_tickets_next_principal = self.purge_old_tickets_next_principal().to_vec();
        let first_principal_bytes = FIRST_PRINCIPAL_BYTES.to_vec();

        if purge_old_tickets_next_principal != first_principal_bytes
            || purge_old_tickets_last_completion_timestamp_nanoseconds + INTERVAL_NANOSECONDS
                <= now_nanoseconds()
        {
            return match self.purge_old_tickets(
                now_nanoseconds(),
                purge_old_tickets_next_principal,
                max_age_in_nanoseconds,
                max_number_to_inspect,
            ) {
                Some(new_next_principal) => {
                    // If a principal is returned then there are some principals that haven't been
                    // checked yet by purge_old_tickets. We record the next principal so that
                    // the next periodic task can continue the work.
                    self.purge_old_tickets_next_principal = Some(new_next_principal);
                    Some(false)
                }
                None => {
                    // If no principal is returned then purge_old_tickets has
                    // exhausted all the tickets.
                    log!(INFO, "purge_old_tickets done");
                    self.purge_old_tickets_next_principal = Some(first_principal_bytes);
                    self.purge_old_tickets_last_completion_timestamp_nanoseconds =
                        Some(now_nanoseconds());
                    Some(true)
                }
            };
        }
        None
    }
```

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L541-548)
```text
message NeuronBasketConstructionParameters {
  // The number of neurons each investor will receive after the
  // decentralization swap. The total tokens swapped for will be
  // evenly distributed across the `count` neurons.
  uint64 count = 1;

  // The amount of additional time it takes for the next neuron to dissolve.
  uint64 dissolve_delay_interval_seconds = 2;
```

**File:** rs/sns/init/src/lib.rs (L1515-1518)
```rust
    /// (9) min_participant_icp_e8s is at least as big as `MIN_PARTICIPANT_ICP_LOWER_BOUND_E8S`.
    ///     This ensures, that users upon calling `swap.refresh_buyer_token()` must participate
    ///     at least `MIN_PARTICIPANT_ICP_LOWER_BOUND_E8S` Hence, no malicious user can overflow
    ///     node's memory by participating with very low amounts.\
```

**File:** rs/sns/swap/canister/swap.did (L474-474)
```text
  finalize_swap : (record {}) -> (FinalizeSwapResponse);
```

**File:** rs/nns/governance/src/neuron_store.rs (L993-1037)
```rust
pub fn groom_some_neurons(
    neuron_store: &mut NeuronStore,
    mut touch_neuron: impl FnMut(&mut Neuron),
    mut next: Bound<NeuronId>,
    mut carry_on: impl FnMut() -> bool,
) -> Bound<NeuronId> {
    // Here, do-while semantics is used, rather than while. I.e. carry_on is
    // only called at the end of the loop, not the beginnin. This results in the
    // nice property that (when there are more neurons), this ALWAYS makes SOME
    // progress.
    loop {
        // Which neuron do we operate on next?
        let current_neuron_id = neuron_store.first_neuron_id(next);

        // If we reached the end, return.
        let current_neuron_id = match current_neuron_id {
            Some(ok) => ok,
            None => {
                // Tell caller to loop back to the beginning of neurons. That
                // way, we keep scanning indefinitely.
                return Bound::Unbounded;
            }
        };

        // Get ready for the next iteration.
        next = Bound::Excluded(current_neuron_id);

        let result = neuron_store.with_neuron_mut(&current_neuron_id, |neuron| {
            touch_neuron(neuron);
        });

        // Log if somehow with_neuron_mut returns Err. This should not be
        // possible, since first_neuron_id must have returned Some in order for
        // this line to be reached.
        if let Err(err) = result {
            println!(
                "{}ERROR: Unable to find neuron {} while pruning following: {:?}",
                LOG_PREFIX, current_neuron_id.id, err,
            );
        }

        if !carry_on() {
            return next;
        }
    }
```
