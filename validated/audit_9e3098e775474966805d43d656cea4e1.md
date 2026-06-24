All cited code is confirmed in the repository. Let me verify the test and the `batch_claim_swap_neurons` flow to complete validation.Audit Report

## Title
Incorrect Status Mapping Causes Already-Claimed SNS Neurons to Be Permanently Blocked from Retry — (`rs/sns/swap/src/types.rs`)

## Summary
In `rs/sns/swap/src/types.rs`, the `From<ClaimedSwapNeuronStatus> for ClaimedStatus` conversion maps `ClaimedSwapNeuronStatus::AlreadyExists` to `ClaimedStatus::Invalid` instead of `ClaimedStatus::Success`. When SNS Governance returns `AlreadyExists` for a neuron that was already created in a prior partial `finalize_swap` run, the Swap canister permanently marks that neuron recipe as `Invalid`, preventing any future retry and causing the SNS finalization sweep to miscount affected neurons indefinitely.

## Finding Description
The root cause is at `rs/sns/swap/src/types.rs` line 1032:

```rust
ClaimedSwapNeuronStatus::AlreadyExists => ClaimedStatus::Invalid,
```

`ClaimedSwapNeuronStatus::AlreadyExists` is returned by SNS Governance (`rs/sns/governance/src/governance.rs` lines 4498–4504) when a neuron already exists in `self.proto.neurons` — meaning it was successfully created in a prior invocation. The semantically correct mapping is `ClaimedStatus::Success`.

The bad status is written back to the recipe at `rs/sns/swap/src/swap.rs` line 1889:
```rust
recipe.claimed_status = Some(claim_status as i32);
```

On any subsequent `finalize_swap` call, `SnsNeuronRecipe::to_neuron_recipe` (lines 3501–3511) encounters `ClaimedStatus::Invalid` and returns `ConversionError::Invalid`, permanently excluding the recipe from all future claim attempts. The recipe is never retried; `sweep_result.invalid` is incremented instead of `sweep_result.success`.

The existing test at lines 4024–4070 explicitly asserts this broken behavior — `ClaimedSwapNeuronStatus::AlreadyExists` is tested as the "invalid case" and the test passes, encoding the bug as intended behavior.

The trigger scenario: (1) `finalize_swap` is called; (2) SNS Governance creates neuron N and returns `Success`; (3) the Swap canister traps or the inter-canister response is not committed before the next message boundary, leaving recipe N still `Pending`; (4) `finalize_swap` is called again; (5) SNS Governance returns `AlreadyExists` for N; (6) Swap maps this to `ClaimedStatus::Invalid` and writes it; (7) all future `finalize_swap` calls permanently skip N as `ConversionError::Invalid`.

## Impact Explanation
Affected neuron recipes are permanently stuck as `Invalid`. The `claim_swap_neurons` sweep counts them as `invalid` rather than `success`, meaning the SNS finalization process reports incorrect outcomes and may never reach a fully-successful sweep state. Participants whose recipes are stuck cannot have their neuron status corrected without manual intervention (the comment at lines 3502–3504 explicitly states "intervention is needed to make valid again" and requires the recipe to be reset to `Pending`). This constitutes a significant SNS protocol harm: the SNS launch finalization is disrupted and participant neuron accounting is permanently corrupted in the Swap canister state. This fits the **High** impact category: *Significant SNS infrastructure security impact with concrete user or protocol harm*.

## Likelihood Explanation
The trigger requires `finalize_swap` to be called more than once with a partial-failure between calls — a scenario the codebase explicitly anticipates (the `finalize_swap_in_progress` lock and `already_tried_to_auto_finalize` flag exist for exactly this reason). Any SNS swap where the Swap canister traps or loses a response after SNS Governance has created neurons but before the Swap canister commits the recipe update will hit this bug. No privileged access is required; any caller can invoke `finalize_swap`.

## Recommendation
Map `ClaimedSwapNeuronStatus::AlreadyExists` to `ClaimedStatus::Success` in `rs/sns/swap/src/types.rs`:

```diff
-            ClaimedSwapNeuronStatus::AlreadyExists => ClaimedStatus::Invalid,
+            ClaimedSwapNeuronStatus::AlreadyExists => ClaimedStatus::Success,
```

Update the corresponding test at lines 4024–4070 to assert `SweepResult { success: 1, .. }` and `ClaimedStatus::Success` for the `AlreadyExists` case.

## Proof of Concept
1. Deploy a test SNS swap in `Committed` state with at least one participant neuron recipe in `Pending` state.
2. Mock SNS Governance's `claim_swap_neurons` to return `ClaimedSwapNeuronStatus::AlreadyExists` for that neuron (simulating a prior partial run).
3. Call `claim_swap_neurons` on the Swap canister.
4. Assert `recipe.claimed_status == ClaimedStatus::Invalid` and `sweep_result.invalid == 1` — both will pass, confirming the bug.
5. Call `claim_swap_neurons` again; assert the recipe is still skipped as `ConversionError::Invalid` and never retried.
6. The existing unit test `test_process_swap_neuron_successful_cases` at `rs/sns/swap/src/swap.rs` lines 3983–4071 already encodes and passes this exact broken behavior, serving as a direct reproducible proof.