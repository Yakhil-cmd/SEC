The code at lines 512–514 of `rs/sns/swap/src/neurons_fund.rs` confirms the bug exactly as described: `value.min_direct_participation_threshold_icp_e8s` is used for both fields. The downstream consumption in `swap.rs` at lines 583–590 is also confirmed. The claim is valid.

Audit Report

## Title
Wrong Field Used in `max_neurons_fund_participation_icp_e8s` Assignment Causes Incorrect Neurons' Fund Participation Cap - (File: rs/sns/swap/src/neurons_fund.rs)

## Summary
In the `From<ValidatedNeuronsFundParticipationConstraints<F>> for NeuronsFundParticipationConstraintsPb` conversion at line 513, `value.min_direct_participation_threshold_icp_e8s` is assigned to `max_neurons_fund_participation_icp_e8s` instead of `value.max_neurons_fund_participation_icp_e8s`. This causes the swap canister to cap Neurons' Fund participation at the minimum threshold value rather than the true maximum, leading to under-counted participation and potential swap failure.

## Finding Description
In `rs/sns/swap/src/neurons_fund.rs` lines 502–527, the `From` conversion sets:

```rust
max_neurons_fund_participation_icp_e8s: Some(
    value.min_direct_participation_threshold_icp_e8s,  // BUG: wrong field
),
```

The resulting `NeuronsFundParticipationConstraintsPb` is stored in the swap's `Init`. Every call to `update_total_participation_amounts` in `swap.rs` (lines 583–590) reads `constraints.max_neurons_fund_participation_icp_e8s` and uses it to cap `neurons_fund_participation_icp_e8s`:

```rust
neurons_fund_participation_icp_e8s.min(
    constraints.max_neurons_fund_participation_icp_e8s.unwrap_or(u64::MAX),
)
```

Because the cap is set to `min_direct_participation_threshold_icp_e8s` (e.g., 36,000 ICP e8s) instead of the true maximum (e.g., 100,000 ICP e8s), the Neurons' Fund contribution is artificially clamped. No existing validation in the `TryFrom<NeuronsFundParticipationConstraintsPb>` path detects this inconsistency because the stored value is a valid `u64`; the semantic error is silent.

## Impact Explanation
This matches the allowed High impact: "Significant SNS security impact with concrete user or protocol harm." Any SNS swap using the matched-funding scheme will have its Neurons' Fund participation under-reported. If the clamped total falls below `min_participants` or `min_icp_e8s`, the swap aborts and the SNS project fails to launch despite sufficient real participation. Neurons' Fund maturity allocated for the swap is wasted, and direct participants lose their opportunity to acquire SNS tokens.

## Likelihood Explanation
The bug is triggered unconditionally whenever NNS governance creates a swap with `neurons_fund_participation = true` and the `From` conversion is exercised. No privileged access is required to trigger `refresh_buyer_tokens`, which calls `update_total_participation_amounts` and reads the corrupted cap. Every SNS swap using matched funding is affected from the moment the swap canister is initialized.

## Recommendation
Change line 513 in `rs/sns/swap/src/neurons_fund.rs` from:
```rust
max_neurons_fund_participation_icp_e8s: Some(
    value.min_direct_participation_threshold_icp_e8s,
),
```
to:
```rust
max_neurons_fund_participation_icp_e8s: Some(
    value.max_neurons_fund_participation_icp_e8s,
),
```
Add a round-trip unit test asserting that `ValidatedNeuronsFundParticipationConstraints → NeuronsFundParticipationConstraintsPb → ValidatedNeuronsFundParticipationConstraints` preserves both `min_direct_participation_threshold_icp_e8s` and `max_neurons_fund_participation_icp_e8s` independently with distinct values.

## Proof of Concept
1. Construct a `ValidatedNeuronsFundParticipationConstraints` with `min_direct_participation_threshold_icp_e8s = 36_000 * E8` and `max_neurons_fund_participation_icp_e8s = 100_000 * E8`.
2. Call `NeuronsFundParticipationConstraintsPb::from(constraints)`.
3. Assert that the resulting protobuf has `max_neurons_fund_participation_icp_e8s == 36_000 * E8` (demonstrating the bug: it should be `100_000 * E8`).
4. Initialize a swap canister with this protobuf in `Init` and call `refresh_buyer_tokens` with direct participation of `50_000 * E8`.
5. Observe that `neurons_fund_participation_icp_e8s` is capped at `36_000 * E8` instead of the matched amount, causing `current_total_participation_e8s()` to under-report and potentially preventing the swap from committing.