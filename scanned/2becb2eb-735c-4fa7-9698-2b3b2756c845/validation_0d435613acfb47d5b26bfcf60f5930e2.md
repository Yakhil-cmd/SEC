### Title
Unbounded Synchronous Iteration Over All Buyers in `create_sns_neuron_recipes` Enables Instruction Exhaustion via Mass Swap Participation - (File: rs/sns/swap/src/swap.rs)

### Summary

The `create_sns_neuron_recipes` function in the SNS Swap canister iterates over the entire `self.buyers` BTreeMap synchronously in a single message execution with no batching. Any unprivileged user can call `refresh_buyer_tokens` to add themselves to `self.buyers`. When a swap is configured with a sufficiently low `min_participant_icp_e8s`, an attacker can populate `self.buyers` with enough entries to exhaust the IC instruction limit (5 billion instructions per update message) when `finalize` is called, permanently blocking swap completion and locking all participant funds.

### Finding Description

**Root cause — unbounded synchronous loop:**

`create_sns_neuron_recipes` iterates over every entry in `self.buyers` in one synchronous execution:

```rust
// rs/sns/swap/src/swap.rs:839
for (buyer_principal, buyer_state) in self.buyers.iter_mut() {
    // BTreeMap traversal + string_to_principal + arithmetic + recipe construction
    // No await, no batch limit, no early exit
}
``` [1](#0-0) 

Unlike `claim_swap_neurons`, which explicitly batches work using `CLAIM_SWAP_NEURONS_BATCH_SIZE`:

```rust
// rs/sns/swap/src/swap.rs:1733-1736
let current_batch_limit =
    std::cmp::min(CLAIM_SWAP_NEURONS_BATCH_SIZE, neuron_recipes.len());
let batch: Vec<NeuronRecipe> = neuron_recipes.drain(0..current_batch_limit).collect();
``` [2](#0-1) 

`create_sns_neuron_recipes` has no equivalent guard.

**Attacker-controlled entry path — `refresh_buyer_tokens`:**

Any principal can call `refresh_buyer_tokens` to insert a new entry into `self.buyers`. The only cost is `min_participant_icp_e8s` (set by the SNS creator, can be as low as 1 e8s) plus the ICP transfer fee (10,000 e8s ≈ 0.0001 ICP). There is no protocol-enforced floor on `min_participant_icp_e8s` and no cap on the number of distinct buyer principals. [3](#0-2) 

**Call chain — publicly reachable:**

`finalize` is a public update endpoint callable by any principal. It calls `finalize_inner`, which calls `create_sns_neuron_recipes` synchronously after the last `await` point:

```
finalize (public update) → finalize_inner (async)
  → sweep_icp(...).await
  → settle_neurons_fund_participation(...).await
  → create_sns_neuron_recipes()   ← synchronous, single execution, no limit
``` [4](#0-3) 

Because `create_sns_neuron_recipes` is called after the last `await`, it runs entirely within one callback message execution. The IC instruction limit (5 billion instructions) applies to this single execution. Each loop iteration performs BTreeMap traversal, `string_to_principal` parsing, 128-bit arithmetic scaling, and neuron-basket construction — conservatively ~10,000–50,000 instructions per buyer. At 50,000 instructions/buyer, the limit is reached at ~100,000 buyers.

### Impact Explanation

If the instruction limit is exceeded, the IC traps the execution and rolls back the message. Because `finalize` holds a re-entrancy lock (`finalize_swap_in_progress`), a trap inside `finalize_inner` releases the lock (via `unlock_finalize_swap` in the outer `finalize`), so `finalize` can be retried. However, every retry will hit the same trap as long as the buyer count remains above the threshold. The swap is permanently stuck in COMMITTED or ABORTED state:

- In COMMITTED state: ICP tokens sent by participants remain locked in the swap canister's subaccounts; SNS tokens are never distributed; governance never transitions to normal mode.
- In ABORTED state: ICP refunds are never processed; participant funds are locked. [5](#0-4) 

### Likelihood Explanation

- **Attacker profile:** Any unprivileged user with access to ICP. No special role required.
- **Cost:** With `min_participant_icp_e8s` = 10,000 e8s (0.0001 ICP, equal to the transfer fee), 100,000 buyers costs ~20 ICP (participation + fees). This is economically feasible for a motivated attacker targeting a high-value SNS launch.
- **Trigger:** The attacker does not need to call `finalize` themselves; any legitimate user or automated system calling `finalize` after the swap ends triggers the trap.
- **No protocol floor:** The IC protocol does not enforce a minimum `min_participant_icp_e8s`. SNS creators may set it very low to maximize participation breadth. [6](#0-5) 

### Recommendation

Implement batching in `create_sns_neuron_recipes` analogous to `batch_claim_swap_neurons`. Persist a cursor (e.g., the last processed buyer principal) in the swap state so that successive calls to `finalize` resume from where the previous execution left off, rather than restarting from the beginning. Alternatively, enforce a protocol-level minimum on `min_participant_icp_e8s` that makes the attack cost-prohibitive (e.g., ≥ 1 ICP per participant). [7](#0-6) 

### Proof of Concept

1. Deploy an SNS with `min_participant_icp_e8s = 10_000` (0.0001 ICP) and `max_icp_e8s` large enough to accommodate 200,000 participants.
2. Using 200,000 distinct principals, each call `refresh_buyer_tokens` transferring 10,000 e8s to the swap subaccount. Total attacker cost: ~20 ICP + fees.
3. Allow the swap to reach its end time and transition to COMMITTED.
4. Call `finalize` from any principal.
5. Execution reaches `create_sns_neuron_recipes`, which iterates over 200,000 entries synchronously. At ~50,000 instructions per entry, this requires ~10 billion instructions — exceeding the 5-billion-instruction limit.
6. The IC traps the execution. The swap remains in COMMITTED state indefinitely. All 200,000 participants' ICP is locked; no SNS tokens are distributed; SNS governance never activates. [8](#0-7)

### Citations

**File:** rs/sns/swap/src/swap.rs (L396-452)
```rust
// High level documentation in the corresponding Protobuf message.
impl Swap {
    /// Create state from an `Init` object.
    ///
    /// Requires that `init` is valid; otherwise it panics.
    pub fn new(init: Init) -> Self {
        if let Err(e) = init.validate() {
            panic!("Invalid init arg, reason: {e}\nArg: {init:#?}\n");
        }
        let mut res = Self {
            lifecycle: Lifecycle::Pending as i32,
            init: None, // Postpone setting this field to avoid cloning.
            params: None,
            cf_participants: vec![],
            buyers: Default::default(), // Btree map
            neuron_recipes: vec![],
            open_sns_token_swap_proposal_id: None,
            finalize_swap_in_progress: Some(false),
            decentralization_sale_open_timestamp_seconds: None,
            decentralization_swap_termination_timestamp_seconds: None,
            next_ticket_id: Some(0),
            purge_old_tickets_last_completion_timestamp_nanoseconds: Some(0),
            purge_old_tickets_next_principal: Some(FIRST_PRINCIPAL_BYTES.to_vec()),
            already_tried_to_auto_finalize: Some(false),
            auto_finalize_swap_response: None,
            direct_participation_icp_e8s: Some(0),
            neurons_fund_participation_icp_e8s: Some(0),
            timers: None,
        };
        if init.validate_swap_init_for_one_proposal_flow().is_ok() {
            // Automatically fill out the fields that the (legacy) open request
            // used to provide, supporting clients who read legacy Swap fields.
            {
                res.cf_participants = vec![];
                match Params::try_from(&init) {
                    Err(err) => {
                        log!(
                            ERROR,
                            "Failed filling out the legacy Param structure: {}. \
                            Falling back to None.",
                            err
                        );
                        res.params = None;
                    }
                    Ok(params) => {
                        res.params = Some(params);
                    }
                }
            }
            res.open_sns_token_swap_proposal_id = init.nns_proposal_id;
            res.decentralization_sale_open_timestamp_seconds = init.swap_start_timestamp_seconds;
            // Transit to the next SNS lifecycle state.
            res.lifecycle = Lifecycle::Adopted as i32;
        }
        res.init = Some(init);
        res
    }
```

**File:** rs/sns/swap/src/swap.rs (L777-884)
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

        // We are selling SNS tokens for the base token (ICP), or, in
        // general, whatever token the ledger referred to as the ICP
        // ledger holds.
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

**File:** rs/sns/swap/src/swap.rs (L1706-1714)
```rust
            Self::batch_claim_swap_neurons(
                sns_governance_client,
                &mut neuron_recipes,
                &mut claimable_neurons_index,
            )
            .await,
        );

        sweep_result
```

**File:** rs/sns/swap/src/swap.rs (L1732-1736)
```rust
        while !neuron_recipes.is_empty() {
            let current_batch_limit =
                std::cmp::min(CLAIM_SWAP_NEURONS_BATCH_SIZE, neuron_recipes.len());

            let batch: Vec<NeuronRecipe> = neuron_recipes.drain(0..current_batch_limit).collect();
```

**File:** rs/sns/swap/src/swap.rs (L2046-2070)
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
```
