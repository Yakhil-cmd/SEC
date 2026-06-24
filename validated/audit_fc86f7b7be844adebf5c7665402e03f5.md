All code references check out. Let me confirm the `require_field_set_and_in_range` behavior and the full validation chain.

The code confirms every claim in the report:

1. **`validate_initial_reward_rate_basis_points`** uses `>` at [1](#0-0)  — `10_000` passes.

2. **`initial_reward_rate_basis_points_defects`** uses `..Self::INITIAL_REWARD_RATE_BASIS_POINTS_CEILING` (exclusive `RangeTo`) at [2](#0-1)  — `10_000` fails.

3. **`require_field_set_and_in_range`** uses `valid_range.contains(value)` at [3](#0-2)  — for `..10_000`, `contains(10_000)` is `false`.

4. **`ManageNervousSystemParameters` proposal validation** calls `new_parameters.inherit_from(current_parameters).validate()` at [4](#0-3) , and `NervousSystemParameters::validate()` calls `validate_voting_rewards_parameters()` at [5](#0-4) , which calls `voting_rewards_parameters.validate()` at [6](#0-5) .

5. **Test gap confirmed**: the test only checks `10_001`, not `10_000` at [7](#0-6) .

---

Audit Report

## Title
`SnsInitPayload::validate_initial_reward_rate_basis_points` Accepts 10_000 That `VotingRewardsParameters::validate` Rejects, Causing SNS Governance Parameter Update DoS - (`rs/sns/init/src/lib.rs`)

## Summary
`SnsInitPayload::validate_initial_reward_rate_basis_points()` uses a strictly-greater-than check (`> 10_000`) that permits `initial_reward_rate_basis_points = 10_000`, while `VotingRewardsParameters::initial_reward_rate_basis_points_defects()` uses an exclusive `RangeTo` (`..10_000`) that rejects this exact value. An SNS initialized with this value passes deployment validation but causes every subsequent `ManageNervousSystemParameters` proposal that does not simultaneously correct the reward rate to be rejected at both proposal submission and execution time, effectively locking SNS governance parameter updates.

## Finding Description
**Root cause — off-by-one in `SnsInitPayload` validator:**

`rs/sns/init/src/lib.rs` L1179–1180 uses `> INITIAL_REWARD_RATE_BASIS_POINTS_CEILING` (i.e., `> 10_000`), so `10_000` returns `Ok(())`.

**Downstream validator uses exclusive upper bound:**

`rs/sns/governance/src/reward.rs` L270–275 passes `..Self::INITIAL_REWARD_RATE_BASIS_POINTS_CEILING` (i.e., `..10_000`) to `require_field_set_and_in_range`. That function calls `valid_range.contains(value)` at L349. Rust's `RangeTo::contains` evaluates `value < end`, so `10_000 < 10_000` is `false` — a defect is returned.

**Full validation chain triggered on every `ManageNervousSystemParameters` proposal:**

At proposal submission, `validate_and_render_manage_nervous_system_parameters` calls `new_parameters.inherit_from(current_parameters).validate()` (`rs/sns/governance/src/proposal.rs` L536). At execution, `perform_manage_nervous_system_parameters` calls `new_params.validate()` (`rs/sns/governance/src/governance.rs` L2595). Both paths reach `NervousSystemParameters::validate()` → `validate_voting_rewards_parameters()` → `VotingRewardsParameters::validate()` → `initial_reward_rate_basis_points_defects()`. If the inherited `initial_reward_rate_basis_points` is `10_000` and the proposal does not override it, validation fails and the proposal is rejected.

**Exploit path:**
1. Deploy SNS with `initial_reward_rate_basis_points = 10_000`. `SnsInitPayload` validation passes.
2. SNS governance canister initializes with these parameters.
3. Submit any `ManageNervousSystemParameters` proposal that does not change `initial_reward_rate_basis_points`. The merged parameters inherit `10_000`, `VotingRewardsParameters::validate()` returns a defect, and the proposal is rejected with `InvalidProposal` / `PreconditionFailed`.
4. All such proposals are rejected until a corrective proposal explicitly sets `initial_reward_rate_basis_points` to a value below `10_000`.

## Impact Explanation
This matches the allowed High impact: **"Significant SNS security impact with concrete user or protocol harm."** Any SNS initialized with `initial_reward_rate_basis_points = 10_000` has its governance parameter update mechanism broken. Token holders cannot change any nervous system parameter (reject cost, voting period, neuron permissions, etc.) via `ManageNervousSystemParameters` proposals until the community identifies the root cause and submits a corrective proposal. Additionally, the 100% annual reward rate causes severe token inflation. The impact is scoped to individual SNS instances but is concrete and directly harmful to SNS governance integrity.

## Likelihood Explanation
The `SnsInitPayload` validator explicitly accepts `10_000` with the message "must be less than or equal to 10000", making it a natural round-number choice for an SNS developer intending to set a 100% initial reward rate. The inconsistency with `VotingRewardsParameters::validate()` is not surfaced at initialization time and only manifests post-launch when governance proposals are attempted. No special privileges beyond SNS deployment are required to trigger the condition.

## Recommendation
Change the boundary check in `SnsInitPayload::validate_initial_reward_rate_basis_points()` from strictly-greater-than to greater-than-or-equal, matching the exclusive upper bound used by `VotingRewardsParameters::initial_reward_rate_basis_points_defects()`:

```rust
// rs/sns/init/src/lib.rs
- if initial_reward_rate_basis_points > VotingRewardsParameters::INITIAL_REWARD_RATE_BASIS_POINTS_CEILING {
+ if initial_reward_rate_basis_points >= VotingRewardsParameters::INITIAL_REWARD_RATE_BASIS_POINTS_CEILING {
```

Alternatively, change `initial_reward_rate_basis_points_defects()` to use `..=CEILING` if 100% is intentionally permitted, and add an explicit test asserting that `10_000` is either accepted or rejected consistently by both validators.

## Proof of Concept
**Unit test (no mainnet required):**

```rust
#[test]
fn test_boundary_inconsistency() {
    // SnsInitPayload accepts 10_000
    let payload = SnsInitPayload {
        initial_reward_rate_basis_points: Some(10_000),
        ..SnsInitPayload::with_valid_values_for_testing()
    };
    assert!(payload.validate_initial_reward_rate_basis_points().is_ok());

    // VotingRewardsParameters rejects 10_000
    let params = VotingRewardsParameters {
        initial_reward_rate_basis_points: Some(10_000),
        ..VotingRewardsParameters::with_default_values()
    };
    assert!(params.validate().is_err()); // fails: 10_000 not in ..10_000

    // ManageNervousSystemParameters proposal inheriting 10_000 is rejected
    let current = NervousSystemParameters::with_default_values();
    // Manually set initial_reward_rate_basis_points to 10_000 in current params
    // then submit a proposal changing only reject_cost_e8s — validate() returns Err
}
```

### Citations

**File:** rs/sns/init/src/lib.rs (L1179-1180)
```rust
        if initial_reward_rate_basis_points
            > VotingRewardsParameters::INITIAL_REWARD_RATE_BASIS_POINTS_CEILING
```

**File:** rs/sns/governance/src/reward.rs (L270-275)
```rust
    fn initial_reward_rate_basis_points_defects(&self) -> Vec<String> {
        require_field_set_and_in_range(
            "initial_reward_rate_basis_points",
            &self.initial_reward_rate_basis_points,
            ..Self::INITIAL_REWARD_RATE_BASIS_POINTS_CEILING,
        )
```

**File:** rs/sns/governance/src/reward.rs (L349-351)
```rust
    if !valid_range.contains(value) {
        result.push(format!("{field_name} not in {valid_range:#?}."));
    }
```

**File:** rs/sns/governance/src/reward.rs (L680-686)
```rust
        assert_is_err!(
            VotingRewardsParameters {
                initial_reward_rate_basis_points: Some(10_001), // > 100%
                ..VOTING_REWARDS_PARAMETERS
            }
            .validate()
        );
```

**File:** rs/sns/governance/src/proposal.rs (L536-536)
```rust
    new_parameters.inherit_from(current_parameters).validate()?;
```

**File:** rs/sns/governance/src/types.rs (L588-588)
```rust
        self.validate_voting_rewards_parameters()?;
```

**File:** rs/sns/governance/src/types.rs (L984-984)
```rust
        voting_rewards_parameters.validate()
```
