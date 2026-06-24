### Title
Unbounded Iteration Over All Buyers in `sweep_icp` / `sweep_sns` Without Instruction-Count Guard — (File: rs/sns/swap/src/swap.rs)

### Summary

The SNS Swap canister's `finalize_swap` flow contains two synchronous loops — `sweep_icp` and `sweep_sns` — that iterate over every buyer / neuron-recipe in the swap's state without any instruction-count checkpoint or batch-size limit. Because `refresh_buyer_tokens` (the cheap "commit" step) only checks a neuron-basket ceiling (`MAX_NEURONS_FOR_DIRECT_PARTICIPANTS`) and not a raw buyer-count ceiling, a swap configured with a small basket size can accumulate a very large number of buyers. When `finalize_swap` is later called, the unbounded loops exhaust the canister's instruction limit, trapping the message and leaving the swap permanently stuck in a half-finalized state behind a held lock.

### Finding Description

**Cheap commit path — `refresh_buyer_tokens`**

`refresh_buyer_tokens` accepts a new participant as long as:

```
(num_direct_participants + 1) * num_sns_neurons_per_basket <= MAX_NEURONS_FOR_DIRECT_PARTICIPANTS
``` [1](#0-0) 

`MAX_NEURONS_FOR_DIRECT_PARTICIPANTS` is a constant from `ic_nervous_system_common`. With a basket size of 1 (the minimum), this allows up to `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS` individual buyers — a very large number. Each `refresh_buyer_tokens` call is cheap: it makes one ledger query and writes one entry to `self.buyers`.

**Expensive execution path — `sweep_icp`**

`sweep_icp` iterates over every entry in `self.buyers` with no instruction guard:

```rust
for (principal_str, buyer_state) in self.buyers.iter_mut() {
    // ... one inter-canister call per buyer ...
    let result = icp_transferable_amount
        .transfer_helper(now_fn, DEFAULT_TRANSFER_FEE, Some(subaccount), &dst, icp_ledger)
        .await;
``` [2](#0-1) 

There is no `break` or instruction-count check anywhere in this loop. The same pattern exists in `sweep_sns`, which iterates over `self.neuron_recipes`: [3](#0-2) 

**`finalize_inner` calls both in sequence**

`finalize_inner` calls `sweep_icp`, then `create_sns_neuron_recipes`, then `sweep_sns`, then `claim_swap_neurons` — all within a single message execution context: [4](#0-3) 

**Lock is held for the entire duration**

`finalize` acquires a lock before calling `finalize_inner` and releases it only after the call returns. If the message traps due to instruction exhaustion, the lock is never released: [5](#0-4) 

### Impact Explanation

If a swap accumulates enough buyers (possible with a basket size of 1 and a high `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS`), calling `finalize_swap` will trap mid-execution due to instruction exhaustion. The finalize lock remains held. Subsequent calls to `finalize_swap` immediately return an error because the lock is already taken. The swap canister is permanently stuck: ICP and SNS tokens are locked in the canister, neurons are never created, and the SNS never transitions to Normal mode. Recovery requires an NNS-approved canister upgrade to clear the lock — a governance-level intervention.

The analog to the SkyWeaver bug is exact: `refresh_buyer_tokens` is the cheap "commit" (like `_commit` in SkyWeaver), and `sweep_icp`/`sweep_sns` are the expensive "mint" (like `mineGolds`). The commit path enforces only a neuron-basket ceiling, not a raw buyer count ceiling that accounts for the per-buyer instruction cost of the sweep loops.

### Likelihood Explanation

Any unprivileged user can call `refresh_buyer_tokens` during the OPEN phase. A swap configured with `neuron_basket_construction_parameters.count = 1` and a low `min_participant_icp_e8s` allows the maximum number of distinct buyers. An attacker (or organic usage) can fill the buyer map to the limit. The `finalize_swap` call is then triggered automatically via the heartbeat (`run_periodic_tasks` → `try_auto_finalize`), requiring no further attacker action. [6](#0-5) 

### Recommendation

Apply the same fix as SkyWeaver PR #9: add a per-call batch limit to `sweep_icp` and `sweep_sns`. Process only a bounded number of buyers per invocation, persist progress, and allow `finalize_swap` to be called repeatedly until all buyers are processed. The existing idempotency markers (`transfer_success_timestamp_seconds`, `has_created_neuron_recipes`) already support resumable sweeps — the missing piece is breaking the loops after a fixed number of iterations (or after an instruction-count threshold, using `ic_cdk::api::instruction_counter()`).

### Proof of Concept

1. Deploy an SNS swap with `neuron_basket_construction_parameters.count = 1` and `min_participant_icp_e8s` set to the minimum.
2. Have N distinct principals each call `refresh_buyer_tokens`, where N approaches `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS`. Each call is cheap and succeeds.
3. Allow the swap to reach COMMITTED state (time-based or by hitting `max_direct_participation_icp_e8s`).
4. Call `finalize_swap` (or wait for the heartbeat to trigger `try_auto_finalize`).
5. `sweep_icp` enters its loop over N buyers, making one inter-canister call per buyer. With N large enough, the message exhausts its instruction limit and traps.
6. The finalize lock (`finalize_swap_in_progress`) remains `true`.
7. All subsequent calls to `finalize_swap` return immediately with `"The swap is already being finalized"` — the swap is permanently stuck. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/sns/swap/src/swap.rs (L1059-1101)
```rust
        else if self.can_auto_finalize().is_ok() {
            // First, record when the finalization started, in case this function is
            // refactored to `await` before this point.
            let auto_finalization_start_seconds = now_fn(false);

            // Then, get the environment
            let environment = self
                .init
                .as_ref()
                .ok_or_else(|| "couldn't get `init`".to_string())
                .and_then(|init| init.environment());

            match environment {
                Err(error) => {
                    log!(
                        ERROR,
                        "Failed to get environment when attempting auto-finalization. Error: {error}"
                    );
                }
                Ok(mut environment) => {
                    // Then, attempt the auto-finalization
                    // `try_auto_finalize` will never return `Error` here
                    // because we already checked `self.can_auto_finalize()`
                    // above, and `try_auto_finalize` will only return an error
                    // if `can_auto_finalize` does.
                    // The FinalizeSwapResponse from finalization will be logged
                    // by `Self::finalize`.
                    if self
                        .try_auto_finalize(now_fn, &mut environment)
                        .await
                        .is_ok()
                    {
                        // The current time is now probably different than the time when
                        // auto-finalization began, due to the `await`.
                        let auto_finalization_finish_seconds = now_fn(true);
                        log!(
                            INFO,
                            "Swap auto-finalization finished at timestamp {auto_finalization_finish_seconds} (started at timestamp {auto_finalization_start_seconds})"
                        );
                    }
                }
            }
        }
```

**File:** rs/sns/swap/src/swap.rs (L1180-1197)
```rust
        {
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

**File:** rs/sns/swap/src/swap.rs (L1505-1531)
```rust
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
```

**File:** rs/sns/swap/src/swap.rs (L1556-1605)
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
```

**File:** rs/sns/swap/src/swap.rs (L2046-2154)
```rust
    pub async fn sweep_icp(
        &mut self,
        now_fn: fn(bool) -> u64,
        icp_ledger: &dyn ICRC1Ledger,
    ) -> SweepResult {
        let lifecycle: Lifecycle = self.lifecycle();

        let init = match self.init_and_validate() {
            Ok(init) => init,
            Err(error_message) => {
                log!(
                    ERROR,
                    "Halting sweep_icp(). State is missing or corrupted: {:?}",
                    error_message
                );
                return SweepResult::new_with_global_failures(1);
            }
        };

        // The following methods are safe to call since we validated Init in the above block
        let sns_governance = init.sns_governance_or_panic();

        let mut sweep_result = SweepResult::default();

        for (principal_str, buyer_state) in self.buyers.iter_mut() {
            // principal_str should always be parseable as a PrincipalId as that is enforced
            // in `refresh_buyer_tokens`. In the case of a bug due to programmer error, increment
            // the invalid field. This will require a manual intervention via an upgrade to correct
            let principal = match string_to_principal(principal_str) {
                Some(p) => p,
                None => {
                    sweep_result.invalid += 1;
                    continue;
                }
            };

            let subaccount = principal_to_subaccount(&principal);
            let dst = if lifecycle == Lifecycle::Committed {
                // This Account should be given a name, such as SNS ICP Treasury...
                Account {
                    owner: sns_governance.get().0,
                    subaccount: None,
                }
            } else {
                Account {
                    owner: principal.0,
                    subaccount: None,
                }
            };

            let icp_transferable_amount = match buyer_state.icp.as_mut() {
                Some(transferable_amount) => transferable_amount,
                // BuyerState.icp should always be present as it is set in `refresh_buyer_tokens`.
                // In the case of a bug due to programmer error, increment the invalid field.
                // This will require a manual intervention via an upgrade to correct
                None => {
                    log!(
                        ERROR,
                        "PrincipalId {} has corrupted BuyerState: {:?}",
                        principal,
                        buyer_state
                    );
                    sweep_result.invalid += 1;
                    continue;
                }
            };

            let result = icp_transferable_amount
                .transfer_helper(
                    now_fn,
                    DEFAULT_TRANSFER_FEE,
                    Some(subaccount),
                    &dst,
                    icp_ledger,
                )
                .await;
            match result {
                // AmountToSmall should never happen as the amount contributed is checked in
                // `refresh_buyer_tokens`. In the case of a bug due to programmer error,
                // increment the invalid field. This will require a manual intervention
                // via an upgrade to correct
                TransferResult::AmountTooSmall => {
                    sweep_result.invalid += 1;
                }
                TransferResult::AlreadyStarted => {
                    sweep_result.skipped += 1;
                }
                TransferResult::Success(_) => {
                    sweep_result.success += 1;
                }
                TransferResult::Failure(_) => {
                    sweep_result.failure += 1;
                }
            }

            // Update the buyer state to indicate funds that have been successfully committed or refunded.
            if result.is_success() {
                // Record transfer fee
                icp_transferable_amount.transfer_fee_paid_e8s =
                    Some(DEFAULT_TRANSFER_FEE.get_e8s());
                // Record the amount minus transfer fee that was refunded or committed.
                let amount_transferred_e8s =
                    Some(icp_transferable_amount.amount_e8s - DEFAULT_TRANSFER_FEE.get_e8s());
                icp_transferable_amount.amount_transferred_e8s = amount_transferred_e8s;
            }
        }

        sweep_result
    }
```

**File:** rs/sns/swap/src/swap.rs (L2165-2200)
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
```
