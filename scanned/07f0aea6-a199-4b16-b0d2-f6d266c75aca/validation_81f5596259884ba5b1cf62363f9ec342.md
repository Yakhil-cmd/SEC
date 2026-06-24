### Title
`SnsInitPayload::validate_initial_reward_rate_basis_points` Allows 100% Reward Rate That Fails `VotingRewardsParameters` Downstream Validation, Breaking SNS Governance Parameter Updates - (`rs/sns/init/src/lib.rs`)

---

### Summary

`SnsInitPayload::validate_initial_reward_rate_basis_points()` uses a strictly-greater-than check (`> 10_000`) that permits `initial_reward_rate_basis_points = 10_000` (100% annual reward rate). However, `VotingRewardsParameters::initial_reward_rate_basis_points_defects()` uses an exclusive upper-bound range (`..CEILING`, i.e., `..10_000`) that rejects this exact value. An SNS initialized with `initial_reward_rate_basis_points = 10_000` passes `SnsInitPayload` validation but permanently fails `VotingRewardsParameters::validate()`, causing every subsequent `ManageNervousSystemParameters` governance proposal that does not simultaneously correct the reward rate to be rejected.

---

### Finding Description

**Root cause — inconsistent boundary in `SnsInitPayload` validator:**

`rs/sns/init/src/lib.rs` line 1179–1188:

```rust
if initial_reward_rate_basis_points
    > VotingRewardsParameters::INITIAL_REWARD_RATE_BASIS_POINTS_CEILING  // 10_000
{
    Err(...)
} else {
    Ok(())   // 10_000 passes — allowed
}
``` [1](#0-0) 

The ceiling constant is defined as:

```rust
pub const INITIAL_REWARD_RATE_BASIS_POINTS_CEILING: u64 = 10_000;
``` [2](#0-1) 

**Downstream validator uses an exclusive upper bound — rejects 10_000:**

`rs/sns/governance/src/reward.rs` line 270–276:

```rust
fn initial_reward_rate_basis_points_defects(&self) -> Vec<String> {
    require_field_set_and_in_range(
        "initial_reward_rate_basis_points",
        &self.initial_reward_rate_basis_points,
        ..Self::INITIAL_REWARD_RATE_BASIS_POINTS_CEILING,  // exclusive: 0..9_999
    )
}
``` [3](#0-2) 

Rust's `RangeTo` (`..N`) is exclusive: `contains(10_000)` returns `false` because `10_000 < 10_000` is false. So `VotingRewardsParameters::validate()` returns a defect for `initial_reward_rate_basis_points = 10_000`.

**`VotingRewardsParameters::validate()` is called during `ManageNervousSystemParameters` proposal validation:**

`NervousSystemParameters::validate()` calls `validate_voting_rewards_parameters()`, which calls `VotingRewardsParameters::validate()`. When a `ManageNervousSystemParameters` proposal is submitted, the governance canister merges the proposed changes with the existing parameters and validates the result. If the existing `initial_reward_rate_basis_points = 10_000` is not explicitly corrected in the proposal, the merged parameters fail `VotingRewardsParameters::validate()`, and the proposal is rejected. [4](#0-3) 

**The existing test only checks 10_001, not 10_000:**

```rust
assert_is_err!(
    VotingRewardsParameters {
        initial_reward_rate_basis_points: Some(10_001), // > 100%
        ..VOTING_REWARDS_PARAMETERS
    }
    .validate()
);
``` [5](#0-4) 

There is no test asserting that `10_000` is rejected by `VotingRewardsParameters::validate()`, masking the inconsistency.

---

### Impact Explanation

1. **Extreme token inflation**: `initial_reward_rate_basis_points = 10_000` sets the annual voting reward rate to 100% of total token supply. `RewardRate::from_basis_points(10_000)` computes `per_year = 10_000 / 10_000 = 1.0`, meaning the entire token supply is minted as rewards each year. This is economically catastrophic for SNS token holders.

2. **Governance parameter update DoS**: Any `ManageNervousSystemParameters` proposal that does not simultaneously change `initial_reward_rate_basis_points` to a value below 10_000 will fail validation. Since governance proposals are the only mechanism to update nervous system parameters, the SNS governance is effectively locked out of parameter changes until a corrective proposal is submitted and adopted — which itself requires the SNS community to recognize the problem. [6](#0-5) 

---

### Likelihood Explanation

An SNS developer configuring `initial_reward_rate_basis_points = 10_000` (100%) is plausible: the `SnsInitPayload` validator explicitly accepts it with the message "must be less than or equal to 10000", and the value is a natural round number. The `SnsInitPayload` validation is the only gate checked before SNS deployment; the inconsistency with `VotingRewardsParameters::validate()` is not surfaced until governance proposals are attempted post-launch.

---

### Recommendation

Change the boundary check in `SnsInitPayload::validate_initial_reward_rate_basis_points()` from strictly-greater-than to greater-than-or-equal, matching the exclusive upper bound used by `VotingRewardsParameters::initial_reward_rate_basis_points_defects()`:

```rust
// rs/sns/init/src/lib.rs
- if initial_reward_rate_basis_points > VotingRewardsParameters::INITIAL_REWARD_RATE_BASIS_POINTS_CEILING {
+ if initial_reward_rate_basis_points >= VotingRewardsParameters::INITIAL_REWARD_RATE_BASIS_POINTS_CEILING {
``` [7](#0-6) 

Alternatively, align `VotingRewardsParameters::initial_reward_rate_basis_points_defects()` to use an inclusive range (`..=CEILING`) if 100% is intentionally permitted, and add an explicit test for the boundary value `10_000` in both validators.

---

### Proof of Concept

```
1. Create an SNS with initial_reward_rate_basis_points = 10_000.
   → SnsInitPayload::validate_initial_reward_rate_basis_points() returns Ok(())
     because 10_000 > 10_000 is false.

2. SNS governance canister is initialized with these parameters.
   → reward_rate_at() does not panic (per_year = 1.0 is valid Decimal),
     but the annual reward rate is 100% of total supply.

3. Submit a ManageNervousSystemParameters proposal to change any parameter
   (e.g., proposal_reject_cost_e8s) without changing initial_reward_rate_basis_points.
   → NervousSystemParameters::validate() calls VotingRewardsParameters::validate()
     on the merged parameters.
   → initial_reward_rate_basis_points_defects() checks 10_000 against ..10_000
     (exclusive), finds it out of range, returns a defect.
   → Proposal is rejected.

4. All ManageNervousSystemParameters proposals that do not correct the reward
   rate are permanently rejected, locking governance parameter updates.
``` [8](#0-7) [9](#0-8)

### Citations

**File:** rs/sns/init/src/lib.rs (L1175-1189)
```rust
    fn validate_initial_reward_rate_basis_points(&self) -> Result<(), String> {
        let initial_reward_rate_basis_points = self
            .initial_reward_rate_basis_points
            .ok_or("Error: initial_reward_rate_basis_points must be specified")?;
        if initial_reward_rate_basis_points
            > VotingRewardsParameters::INITIAL_REWARD_RATE_BASIS_POINTS_CEILING
        {
            Err(format!(
                "Error: initial_reward_rate_basis_points must be less than or equal to {}",
                VotingRewardsParameters::INITIAL_REWARD_RATE_BASIS_POINTS_CEILING
            ))
        } else {
            Ok(())
        }
    }
```

**File:** rs/sns/governance/src/reward.rs (L119-127)
```rust
impl RewardRate {
    pub fn from_basis_points(basis_points: u64) -> Self {
        let per_year = i2d(basis_points) / i2d(10_000);
        Self { per_year }
    }

    fn per_day(&self) -> Decimal {
        self.per_year / *NOMINAL_DAYS_PER_YEAR
    }
```

**File:** rs/sns/governance/src/reward.rs (L145-145)
```rust
    pub const INITIAL_REWARD_RATE_BASIS_POINTS_CEILING: u64 = 10_000;
```

**File:** rs/sns/governance/src/reward.rs (L160-173)
```rust
    pub fn validate(&self) -> Result<(), String> {
        let mut defects = vec![];

        defects.append(&mut self.round_duration_seconds_defects());
        defects.append(&mut self.reward_rate_transition_duration_seconds_defects());
        defects.append(&mut self.initial_reward_rate_basis_points_defects());
        defects.append(&mut self.final_reward_rate_basis_points_defects());

        if defects.is_empty() {
            Ok(())
        } else {
            Err(defects.join("\n"))
        }
    }
```

**File:** rs/sns/governance/src/reward.rs (L262-276)
```rust
    fn reward_rate_transition_duration_seconds_defects(&self) -> Vec<String> {
        require_field_set_and_in_range(
            "reward_rate_transition_duration_seconds",
            &self.reward_rate_transition_duration_seconds,
            0..,
        )
    }

    fn initial_reward_rate_basis_points_defects(&self) -> Vec<String> {
        require_field_set_and_in_range(
            "initial_reward_rate_basis_points",
            &self.initial_reward_rate_basis_points,
            ..Self::INITIAL_REWARD_RATE_BASIS_POINTS_CEILING,
        )
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
