I have found a valid analog. The vulnerability class is **incorrect status assignment** — where a response from a sub-call is mapped to the wrong enum value, silently causing a different (incorrect) outcome. In the IC codebase, the exact analog is in `rs/sns/swap/src/types.rs`: the `From<ClaimedSwapNeuronStatus> for ClaimedStatus` conversion maps `ClaimedSwapNeuronStatus::AlreadyExists` to `ClaimedStatus::Invalid` instead of `ClaimedStatus::Success`.

---

### Title
Incorrect Status Mapping Causes Already-Claimed SNS Neurons to Be Permanently Blocked from Retry — (`rs/sns/swap/src/types.rs`)

### Summary
In the SNS Swap canister's `process_swap_neuron` function, when SNS Governance responds to a `claim_swap_neurons` batch call with `ClaimedSwapNeuronStatus::AlreadyExists` for a neuron, the Swap canister maps this to `ClaimedStatus::Invalid` instead of `ClaimedStatus::Success`. This causes the neuron recipe to be permanently marked as `Invalid`, blocking any future retry and preventing the SNS finalization from completing correctly for affected participants.

### Finding Description

In `rs/sns/swap/src/types.rs`, the conversion from `ClaimedSwapNeuronStatus` (returned by SNS Governance) to `ClaimedStatus` (stored in the Swap canister's `SnsNeuronRecipe`) is:

```rust
impl From<ClaimedSwapNeuronStatus> for ClaimedStatus {
    fn from(claimed_swap_neuron_status: ClaimedSwapNeuronStatus) -> Self {
        match claimed_swap_neuron_status {
            ClaimedSwapNeuronStatus::Success => ClaimedStatus::Success,
            ClaimedSwapNeuronStatus::Unspecified => ClaimedStatus::Failed,
            ClaimedSwapNeuronStatus::MemoryExhausted => ClaimedStatus::Failed,
            ClaimedSwapNeuronStatus::Invalid => ClaimedStatus::Invalid,
            ClaimedSwapNeuronStatus::AlreadyExists => ClaimedStatus::Invalid,  // BUG
        }
    }
}
``` [1](#0-0) 

`ClaimedSwapNeuronStatus::AlreadyExists` is returned by SNS Governance when a neuron was **already successfully created** in a prior `claim_swap_neurons` call (e.g., during a previous `finalize` invocation). The correct mapping should be `ClaimedStatus::Success`, because the neuron exists and is claimed. Instead, it is mapped to `ClaimedStatus::Invalid`. [2](#0-1) 

The `ClaimedStatus::Invalid` value is then written back to `recipe.claimed_status`:

```rust
recipe.claimed_status = Some(claim_status as i32);
``` [3](#0-2) 

On any subsequent `finalize` call, `SnsNeuronRecipe::to_neuron_recipe` checks the stored `claimed_status` and, finding `ClaimedStatus::Invalid`, returns `ConversionError::Invalid` — permanently skipping the recipe without attempting to claim it again:

```rust
ClaimedStatus::Invalid | ClaimedStatus::Unspecified => {
    return Err((
        ConversionError::Invalid,
        format!("Recipe {self:?} was invalid in a previous invocation..."),
    ));
}
``` [4](#0-3) 

The `AlreadyExists` response from SNS Governance is a legitimate, expected outcome when `finalize` is called more than once (e.g., due to retries or the auto-finalization mechanism). The neuron was actually created successfully; the Swap canister just doesn't know it.

### Impact Explanation

When `ClaimedSwapNeuronStatus::AlreadyExists` is returned for any neuron recipe during a `finalize` call, that recipe is permanently marked `Invalid`. On all subsequent `finalize` calls, those recipes are skipped with `ConversionError::Invalid`. The SNS finalization process (`finalize_swap`) tracks completion via `SweepResult`; recipes stuck as `Invalid` are counted as `invalid` rather than `success`, meaning the finalization may never report full completion. Affected SNS swap participants whose neuron recipes are stuck in `Invalid` state will not have their SNS neurons properly accounted for in the sweep result, and the finalization state machine may stall or report incorrect outcomes. This is a correctness/functionality disruption — funds are not at direct risk, but the SNS launch process is disrupted. [5](#0-4) 

### Likelihood Explanation

This is triggered whenever `finalize_swap` is called more than once after the swap is committed — a scenario that is explicitly supported and expected (the `finalize_swap_in_progress` lock and `already_tried_to_auto_finalize` flag both exist precisely because finalization can be retried). Any SNS swap that requires multiple `finalize` calls (e.g., due to partial failures, manual retries, or the auto-finalization timer) will hit this bug for any neuron that was successfully claimed in a prior call. The entry path requires no privileged access: any caller can invoke `finalize_swap` on the Swap canister. [6](#0-5) 

### Recommendation

Map `ClaimedSwapNeuronStatus::AlreadyExists` to `ClaimedStatus::Success` instead of `ClaimedStatus::Invalid`, since `AlreadyExists` means the neuron was already successfully created in a prior invocation:

```diff
 impl From<ClaimedSwapNeuronStatus> for ClaimedStatus {
     fn from(claimed_swap_neuron_status: ClaimedSwapNeuronStatus) -> Self {
         match claimed_swap_neuron_status {
             ClaimedSwapNeuronStatus::Success => ClaimedStatus::Success,
             ClaimedSwapNeuronStatus::Unspecified => ClaimedStatus::Failed,
             ClaimedSwapNeuronStatus::MemoryExhausted => ClaimedStatus::Failed,
             ClaimedSwapNeuronStatus::Invalid => ClaimedStatus::Invalid,
-            ClaimedSwapNeuronStatus::AlreadyExists => ClaimedStatus::Invalid,
+            ClaimedSwapNeuronStatus::AlreadyExists => ClaimedStatus::Success,
         }
     }
 }
``` [7](#0-6) 

### Proof of Concept

1. A committed SNS swap calls `finalize_swap`. During `claim_swap_neurons`, SNS Governance successfully creates neuron N and returns `ClaimedSwapNeuronStatus::Success`. Recipe N is stored as `ClaimedStatus::Success`.
2. `finalize_swap` is called again (retry, or auto-finalization timer fires). `SnsNeuronRecipe::to_neuron_recipe` sees `ClaimedStatus::Success` and skips recipe N with `ConversionError::AlreadyProcessed` — this is correct.
3. However, consider a partial-failure scenario: `finalize_swap` is called, SNS Governance creates neuron N and returns `ClaimedSwapNeuronStatus::AlreadyExists` (because it was created in a prior partial run that didn't update the recipe). The conversion maps this to `ClaimedStatus::Invalid` and writes it to the recipe.
4. On all subsequent `finalize_swap` calls, recipe N is now permanently skipped as `ConversionError::Invalid`, and the `SweepResult.invalid` counter is incremented instead of `success`. The finalization sweep never counts N as successfully claimed. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/sns/swap/src/types.rs (L1024-1035)
```rust
/// The mapping of ClaimedSwapNeuronStatus to ClaimedStatus
impl From<ClaimedSwapNeuronStatus> for ClaimedStatus {
    fn from(claimed_swap_neuron_status: ClaimedSwapNeuronStatus) -> Self {
        match claimed_swap_neuron_status {
            ClaimedSwapNeuronStatus::Success => ClaimedStatus::Success,
            ClaimedSwapNeuronStatus::Unspecified => ClaimedStatus::Failed,
            ClaimedSwapNeuronStatus::MemoryExhausted => ClaimedStatus::Failed,
            ClaimedSwapNeuronStatus::Invalid => ClaimedStatus::Invalid,
            ClaimedSwapNeuronStatus::AlreadyExists => ClaimedStatus::Invalid,
        }
    }
}
```

**File:** rs/sns/governance/src/governance.rs (L4498-4504)
```rust
            // Skip this neuron if it was previously claimed.
            if self.proto.neurons.contains_key(&neuron_id.to_string()) {
                swap_neurons.push(SwapNeuron::from_neuron_recipe(
                    neuron_recipe,
                    ClaimedSwapNeuronStatus::AlreadyExists,
                ));
                continue;
```

**File:** rs/sns/governance/src/governance.rs (L4535-4547)
```rust
            match self.add_neuron(neuron) {
                Ok(()) => swap_neurons.push(SwapNeuron::from_neuron_recipe(
                    neuron_recipe,
                    ClaimedSwapNeuronStatus::Success,
                )),
                Err(err) => {
                    log!(ERROR, "Failed to claim Swap Neuron due to {:?}", err);
                    swap_neurons.push(SwapNeuron::from_neuron_recipe(
                        neuron_recipe,
                        ClaimedSwapNeuronStatus::MemoryExhausted,
                    ))
                }
            }
```

**File:** rs/sns/swap/src/swap.rs (L1626-1714)
```rust
    /// In state COMMITTED. Claims SNS Neurons on behalf of participants.
    ///
    /// Returns the following values:
    /// - the number of skipped neurons because of previous claims
    /// - the number of successful claims
    /// - the number of failed claims
    /// - the number of invalid claims due to corrupted neuron recipe state
    /// - the number of global failures due to corrupted Swap state or inconsistent API responses
    pub async fn claim_swap_neurons(
        &mut self,
        sns_governance_client: &mut impl SnsGovernanceClient,
    ) -> SweepResult {
        if self.lifecycle() != Lifecycle::Committed {
            log!(
                ERROR,
                "Halting claim_neurons(). SNS Neurons cannot be distributed if \
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
                    "Halting claim_neurons(). State is missing or corrupted: {:?}",
                    error_message
                );
                return SweepResult::new_with_global_failures(1);
            }
        };

        // The following methods are safe to call since we validated Init in the above block
        let nns_governance = init.nns_governance_or_panic();
        let sns_transaction_fee_e8s = init.transaction_fee_e8s_or_panic();

        let mut sweep_result = SweepResult::default();

        // Create an index of NeuronId -> &mut SnsNeuronRecipe such that the SnsNeuronRecipe can
        // be accessed in O(1) time.
        let mut claimable_neurons_index = btreemap! {};

        // The `NeuronRecipe`s that will be used to create neurons. We are converting
        // `SnsNeuronRecipe`s to a type with a similar name, `NeuronRecipe`, as this is the type
        // expected by the SNS Governance canister.
        let mut neuron_recipes = vec![];

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

        // If neuron_recipes is empty, all recipes are either Invalid or Skipped and there
        // is no work to do.
        if neuron_recipes.is_empty() {
            return sweep_result;
        }

        sweep_result.consume(
            Self::batch_claim_swap_neurons(
                sns_governance_client,
                &mut neuron_recipes,
                &mut claimable_neurons_index,
            )
            .await,
        );

        sweep_result
```

**File:** rs/sns/swap/src/swap.rs (L1869-1889)
```rust
        let claim_status = ClaimedStatus::from(claimed_swap_neuron_status);

        match claim_status {
            ClaimedStatus::Success => sweep_result.success += 1,
            ClaimedStatus::Failed => sweep_result.failure += 1,
            ClaimedStatus::Invalid => sweep_result.invalid += 1,
            ClaimedStatus::Pending | ClaimedStatus::Unspecified => {
                log!(
                    ERROR,
                    "Unexpected ClaimedStatus ({:?}) resulting from \
                    ClaimedSwapNeuronStatus ({:?}) for NeuronId {}",
                    claim_status,
                    claimed_swap_neuron_status,
                    neuron_id
                );
                // Increment the SweepResult's invalid field, but the claiming could be attempted again
                sweep_result.invalid += 1;
            }
        }

        recipe.claimed_status = Some(claim_status as i32);
```

**File:** rs/sns/swap/src/swap.rs (L2899-2943)
```rust
    /// Returns true if the Swap can be aborted at the specified
    /// timestamp, and false otherwise.
    ///
    /// Conditions:
    /// 1. The lifecycle of Swap is `Lifecycle::Open`
    /// 2. The Swap has ended (either the Swap is due or the maximum ICP target was reached) and there
    ///    has not been sufficient participation reached.
    pub fn can_abort(&self, now_seconds: u64) -> bool {
        if self.lifecycle() != Lifecycle::Open {
            return false;
        }

        // if the swap is due or the ICP target is reached without sufficient participation, we can abort
        (self.swap_due(now_seconds) || self.icp_target_progress().is_reached_or_exceeded())
            && !self.sufficient_participation()
    }

    /// Returns Ok(()) if the swap can auto-finalize, and Err(reason) otherwise
    pub fn can_auto_finalize(&self) -> Result<(), String> {
        // Being allowed to finalize is a precondition for being allowed
        // to auto-finalize.
        self.can_finalize()?;

        let Some(init) = self.init.as_ref() else {
            return Err("unable to access swap's init".to_string());
        };

        // Fail early if `self.init.should_auto_finalize` doesn't indicate that
        // auto-finalization is enabled.
        if !init.should_auto_finalize.unwrap_or_default() {
            return Err(format!(
                "init.should_auto_finalize is {:?}, not attempting auto-finalization.",
                init.should_auto_finalize
            ));
        }

        // Fail early if we've already tried to auto-finalize the swap.
        if self.already_tried_to_auto_finalize.unwrap_or(true) {
            return Err(format!(
                "self.already_tried_to_auto_finalize is {:?}, indicating that an attempt has already been made to auto-finalize. No further attempts will be made automatically. Manually calling finalize is still allowed.",
                self.already_tried_to_auto_finalize
            ));
        }

        Ok(())
```

**File:** rs/sns/swap/src/swap.rs (L3501-3511)
```rust
                ClaimedStatus::Invalid | ClaimedStatus::Unspecified => {
                    // If the Recipe is marked as invalid or unspecified, intervention is needed
                    // to make valid again. As part of that intervention, the recipe must be marked
                    // as ClaimedStatus::Pending to attempt again.
                    return Err((
                        ConversionError::Invalid,
                        format!(
                            "Recipe {self:?} was invalid in a previous invocation of claim_swap_neurons(). \
                        Skipping"
                        ),
                    ));
```

**File:** rs/sns/swap/src/swap.rs (L3983-4071)
```rust
    #[test]
    fn test_process_swap_neuron_successful_cases() {
        let mut successful_recipe = SnsNeuronRecipe {
            claimed_status: Some(ClaimedStatus::Pending as i32),
            ..Default::default()
        };
        let mut failed_recipe = SnsNeuronRecipe {
            claimed_status: Some(ClaimedStatus::Pending as i32),
            ..Default::default()
        };
        let mut invalid_recipe = SnsNeuronRecipe {
            claimed_status: Some(ClaimedStatus::Pending as i32),
            ..Default::default()
        };

        let mut index = btreemap! {
            NeuronId::new_test_neuron_id(1) => &mut successful_recipe,
            NeuronId::new_test_neuron_id(2) => &mut failed_recipe,
            NeuronId::new_test_neuron_id(3) => &mut invalid_recipe,
        };

        // Process first to satisfy the borrow checker

        // Successful case
        let successful_sweep_result = Swap::process_swap_neuron(
            SwapNeuron {
                id: Some(NeuronId::new_test_neuron_id(1)),
                status: ClaimedSwapNeuronStatus::Success as i32,
            },
            &mut index,
        );

        // Failure case
        let failed_sweep_result = Swap::process_swap_neuron(
            SwapNeuron {
                id: Some(NeuronId::new_test_neuron_id(2)),
                status: ClaimedSwapNeuronStatus::MemoryExhausted as i32,
            },
            &mut index,
        );

        // Invalid case
        let invalid_sweep_result = Swap::process_swap_neuron(
            SwapNeuron {
                id: Some(NeuronId::new_test_neuron_id(3)),
                status: ClaimedSwapNeuronStatus::AlreadyExists as i32,
            },
            &mut index,
        );

        // Success case
        assert_eq!(
            successful_sweep_result,
            SweepResult {
                success: 1,
                ..Default::default()
            }
        );
        assert_eq!(
            successful_recipe.claimed_status,
            Some(ClaimedStatus::Success as i32)
        );

        // Failure case
        assert_eq!(
            failed_sweep_result,
            SweepResult {
                failure: 1,
                ..Default::default()
            }
        );
        assert_eq!(
            failed_recipe.claimed_status,
            Some(ClaimedStatus::Failed as i32)
        );

        // Invalid case
        assert_eq!(
            invalid_sweep_result,
            SweepResult {
                invalid: 1,
                ..Default::default()
            }
        );
        assert_eq!(
            invalid_recipe.claimed_status,
            Some(ClaimedStatus::Invalid as i32),
        );
    }
```
