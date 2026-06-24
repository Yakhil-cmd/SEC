Audit Report

## Title
`ClaimedSwapNeuronStatus::AlreadyExists` Incorrectly Mapped to `ClaimedStatus::Invalid`, Permanently Halting SNS Swap Finalization - (File: rs/sns/swap/src/types.rs)

## Summary
In `rs/sns/swap/src/types.rs`, the `From<ClaimedSwapNeuronStatus> for ClaimedStatus` impl maps `AlreadyExists` to `ClaimedStatus::Invalid` instead of `ClaimedStatus::Success`. Because `AlreadyExists` means SNS Governance already committed the neuron, this is semantically a success. The incorrect mapping causes `is_successful_sweep()` to return `false`, which sets an error message and permanently halts `finalize_inner` before `set_sns_governance_to_normal_mode` is ever called. On every subsequent retry, the affected recipes are permanently skipped as `ConversionError::Invalid`, leaving SNS Governance stuck in restricted mode indefinitely.

## Finding Description

**Root cause** — `rs/sns/swap/src/types.rs` line 1032: [1](#0-0) 

`AlreadyExists` is mapped to `ClaimedStatus::Invalid` instead of `ClaimedStatus::Success`.

**Trigger path:**

1. `finalize_swap` (publicly callable) invokes `claim_swap_neurons` in batches. [2](#0-1) 

2. A batch is sent to SNS Governance, which commits the neurons and returns success. However, a `CanisterCallError` occurs on the response path. The Swap canister increments `global_failures` and returns early — the comment explicitly acknowledges the rollback is not guaranteed ("we hope that the canister being called rolls back"): [3](#0-2) 

3. On retry, `to_neuron_recipe()` re-includes those `Pending` neurons. SNS Governance, having already committed them, returns `ClaimedSwapNeuronStatus::AlreadyExists`: [4](#0-3) 

4. `process_swap_neuron` converts `AlreadyExists` → `ClaimedStatus::Invalid`, increments `sweep_result.invalid`, and writes `ClaimedStatus::Invalid` to the recipe: [5](#0-4) 

5. `is_successful_sweep()` returns `false` because `invalid > 0`: [6](#0-5) 

6. `set_claim_neuron_result` sets an error message: [7](#0-6) 

7. `finalize_inner` returns early, never reaching `set_sns_governance_to_normal_mode`: [8](#0-7) 

8. On every subsequent retry, `to_neuron_recipe()` sees `ClaimedStatus::Invalid` and permanently returns `ConversionError::Invalid`, re-incrementing `sweep_result.invalid` each time — the state is unrecoverable without a manual canister upgrade: [9](#0-8) 

## Impact Explanation

SNS Governance is initialized in restricted mode during the swap and only transitions to `Normal` mode via `set_sns_governance_to_normal_mode` at the end of `finalize_inner`. If finalization is permanently halted, SNS Governance never exits restricted mode. All governance operations — proposals, voting, neuron management — are blocked for all SNS participants. This constitutes a permanent, protocol-level DoS of the SNS governance system, matching the allowed impact: **High — Significant SNS infrastructure security impact with concrete user and protocol harm.** Recovery requires a privileged manual canister upgrade to reset affected `SnsNeuronRecipe.claimed_status` fields from `Invalid` back to `Pending`.

## Likelihood Explanation

The trigger requires a `CanisterCallError` on the response path of a `claim_swap_neurons` inter-canister call where SNS Governance already committed the batch. The code itself acknowledges this is possible ("we hope that the canister being called rolls back"). Causes include response encoding failures, subnet memory pressure during response delivery, or transient system-level issues — all realistic in production. No privileged access is required; `finalize_swap` is a public `#[update]` endpoint callable by any principal. The retry path is also publicly triggerable, meaning any caller can repeatedly drive the swap into the permanent failure state once the initial `CanisterCallError` occurs.

## Recommendation

In `rs/sns/swap/src/types.rs`, change the mapping for `AlreadyExists` from `ClaimedStatus::Invalid` to `ClaimedStatus::Success`:

```rust
ClaimedSwapNeuronStatus::AlreadyExists => ClaimedStatus::Success,
```

A neuron that already exists in SNS Governance is, from the Swap canister's perspective, successfully claimed. This also requires updating the test `test_process_swap_neuron_successful_cases` in `rs/sns/swap/src/swap.rs` (lines 4024–4070), which currently encodes the incorrect behavior as expected. [10](#0-9) 

## Proof of Concept

The existing test at `rs/sns/swap/src/swap.rs:4024–4070` directly encodes the bug as expected behavior: it asserts `ClaimedSwapNeuronStatus::AlreadyExists` produces `SweepResult { invalid: 1, .. }` and sets `claimed_status` to `ClaimedStatus::Invalid`. This test passes today and confirms the incorrect mapping is live in production code.

A deterministic integration test reproducing the full failure chain:
1. Deploy SNS swap in `Committed` state with N neuron recipes.
2. Call `finalize_swap`. Inject a mock `SnsGovernanceClient` that returns `Ok(ClaimSwapNeuronsResponse { swap_neurons: [AlreadyExists, ...] })` for the claim batch (simulating the retry-after-CanisterCallError scenario).
3. Assert `finalize_swap_response.has_error_message() == true` and `claim_neuron_result.invalid > 0`.
4. Call `finalize_swap` again. Assert the same recipes are now permanently skipped (`ConversionError::Invalid`) and `set_sns_governance_to_normal_mode` is never invoked.
5. Confirm SNS Governance mode remains `RestrictedTo(Swap)` indefinitely.

### Citations

**File:** rs/sns/swap/src/types.rs (L921-928)
```rust
    pub fn set_claim_neuron_result(&mut self, claim_neuron_result: SweepResult) {
        if !claim_neuron_result.is_successful_sweep() {
            self.set_error_message(
                "Claiming SNS Neurons did not complete fully, some claims were invalid or failed. Halting swap finalization".to_string()
            );
        }
        self.claim_neuron_result = Some(claim_neuron_result);
    }
```

**File:** rs/sns/swap/src/types.rs (L968-978)
```rust
impl SweepResult {
    fn is_successful_sweep(&self) -> bool {
        let SweepResult {
            failure,
            invalid,
            success: _,
            skipped: _,
            global_failures,
        } = self;
        *failure == 0 && *invalid == 0 && *global_failures == 0
    }
```

**File:** rs/sns/swap/src/types.rs (L1025-1035)
```rust
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

**File:** rs/sns/swap/src/swap.rs (L1602-1612)
```rust
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
```

**File:** rs/sns/swap/src/swap.rs (L1754-1769)
```rust
                Err(canister_call_error) => {
                    // The canister_call_error indicates a trap in the callback function, which
                    // could be the result of an unexpected panic in SNS Governance or an issue
                    // with the underlying Canister or Replica. As it is a CanisterCallError
                    // we hope that the canister being called rolls back to the appropriate checkpoint.
                    // The swap canister will mark the current batch and remaining neurons as failed
                    // and return. Calling finalize again will result in another attempt to
                    // claim those neurons.
                    log!(
                        ERROR,
                        "Encountered a CanisterCallError when claiming a batch of neurons. Err: {:?}",
                        canister_call_error,
                    );
                    sweep_result.global_failures += 1;
                    return sweep_result;
                }
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

**File:** rs/sns/swap/src/swap.rs (L4024-4070)
```rust
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
```

**File:** rs/sns/governance/src/governance.rs (L4498-1505)
```rust

```
