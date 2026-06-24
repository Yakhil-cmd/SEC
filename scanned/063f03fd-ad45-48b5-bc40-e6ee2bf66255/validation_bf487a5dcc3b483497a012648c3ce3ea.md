### Title
SNS `INITIAL_REWARD_RATE_BASIS_POINTS_CEILING` Allows 100% Annual Token Inflation, Enabling Extreme Dilution of Swap Participants - (File: `rs/sns/governance/src/reward.rs`)

---

### Summary

The SNS governance module permits an `initial_reward_rate_basis_points` of up to `10_000` (100% annual inflation). An SNS creator can set this at launch, causing the SNS token supply to double every year and massively diluting all swap participants. The code itself acknowledges the ceiling is unreasonably permissive.

---

### Finding Description

`VotingRewardsParameters::INITIAL_REWARD_RATE_BASIS_POINTS_CEILING` is set to `10_000`, representing 100% annual token inflation in basis points (1 basis point = 0.01%). [1](#0-0) 

The SNS init validation in `rs/sns/init/src/lib.rs` uses a strict-greater-than check (`>`), meaning values **up to and including** `10_000` pass validation: [2](#0-1) 

The governance-side validation uses an **exclusive** range (`..Self::INITIAL_REWARD_RATE_BASIS_POINTS_CEILING`), meaning it only allows 0–9,999: [3](#0-2) 

This creates an off-by-one inconsistency: the SNS init path allows `10_000` (100%), while the governance runtime path would reject it. More critically, even 9,999 basis points (~100% annual inflation) is an unreasonably high ceiling that the code itself flags:

> *"Some 'highly not sensible' values are allowed (e.g. 90% growth rate) simply because the transition between 'sensible' and 'insane' is gradual, not hard."* [4](#0-3) 

The `initial_reward_rate_basis_points` field is part of `SnsInitPayload`, submitted by an SNS creator as part of an NNS governance proposal: [5](#0-4) 

---

### Impact Explanation

An SNS creator sets `initial_reward_rate_basis_points = 10_000` (or 9_999) in the `SnsInitPayload`. Once the NNS approves the SNS launch proposal (a routine governance action), the SNS mints voting rewards at up to 100% of the total token supply per year. All participants who purchased SNS tokens in the swap are immediately subject to extreme dilution. Within one year, the token supply doubles; within a few years, early swap participants hold a negligible fraction of the supply. This constitutes a ledger conservation / token economics violation: the SNS token's value is systematically destroyed by protocol-enforced minting at an unreasonably high rate, harming all swap participants who contributed ICP.

---

### Likelihood Explanation

An SNS creator is an unprivileged developer who submits an NNS proposal. NNS voters reviewing SNS launch proposals are unlikely to scrutinize the numeric value of `initial_reward_rate_basis_points` carefully, especially since the field is buried in a large `SnsInitPayload`. The ceiling of `10_000` is explicitly permitted by the validation code, so no error is raised. The attack requires no privileged access, no key compromise, and no majority corruption — only a standard SNS launch proposal.

---

### Recommendation

Lower `INITIAL_REWARD_RATE_BASIS_POINTS_CEILING` to a reasonable maximum, analogous to the NNS's own reward rate (which starts at ~10% = 1,000 basis points). A ceiling of `1_000` to `2_500` basis points (10%–25% annual inflation) would still allow generous incentives while preventing extreme dilution. Additionally, fix the off-by-one inconsistency between the SNS init validation (`>`) and the governance runtime validation (`..` exclusive range) so both paths enforce the same bound. [1](#0-0) [2](#0-1) 

---

### Proof of Concept

1. An SNS creator constructs an `SnsInitPayload` with `initial_reward_rate_basis_points: Some(10_000)`.
2. The `validate_initial_reward_rate_basis_points` check passes because `10_000 > 10_000` is `false`.
3. The NNS governance proposal to launch the SNS is submitted and approved by NNS voters.
4. The SNS is deployed. The `VotingRewardsParameters` in the SNS governance canister has `initial_reward_rate_basis_points = 10_000`.
5. Each reward round, the SNS governance mints tokens proportional to `token_supply * (10_000 / 10_000) * round_duration / year = token_supply * 1.0 * fraction_of_year`.
6. Over one year, the total token supply doubles. Swap participants who paid ICP for SNS tokens now hold half the fraction of the supply they originally purchased, with no recourse. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/sns/governance/src/reward.rs (L141-145)
```rust
impl VotingRewardsParameters {
    /// This is an upper bound for `initial_reward_rate_basis_points_ceiling`. High values
    /// may improve the incentives when voting, but too-high values may also lead
    /// to an over-concentration of voting power and high inflation.
    pub const INITIAL_REWARD_RATE_BASIS_POINTS_CEILING: u64 = 10_000;
```

**File:** rs/sns/governance/src/reward.rs (L154-159)
```rust
    /// Each field has a range of allowed values. Those limits are just sanity
    /// checks. All "sensible" values are allowed.
    ///
    /// Some "highly not sensible" values are allowed (e.g. 90% growth rate)
    /// simply because the transition between "sensible" and "insane" is
    /// gradual, not hard.
```

**File:** rs/sns/governance/src/reward.rs (L197-241)
```rust
    pub fn reward_rate_at(&self, now: Instant) -> RewardRate {
        let reward_rate_transition_duration_seconds = self
            .reward_rate_transition_duration_seconds
            .expect("reward_rate_transition_duration_seconds unset");

        let time_since_genesis = {
            let result = now - *GENESIS;
            // For the purposes of determining reward rate, treat times before
            // genesis the same as at genesis. This is not expected to occur in
            // practice. This code is just being extra defensive.
            if result.as_secs() < i2d(0) {
                Duration { days: i2d(0) }
            } else {
                result
            }
        };
        if reward_rate_transition_duration_seconds == 0
            || time_since_genesis.as_secs() >= i2d(reward_rate_transition_duration_seconds)
        {
            return self.final_reward_rate();
        }

        // s linearly varies from 1 -> 0 as seconds_since_genesis varies from 0
        // to reward_rate_transition_duration_seconds.
        let transition = LinearMap::new(
            dec!(0)..i2d(reward_rate_transition_duration_seconds),
            dec!(1)..dec!(0),
        );
        let s = transition.apply(time_since_genesis.as_secs());
        // s2 varies quadratically from 1 -> 0 (again, as seconds_since_genesis
        // varies from 0 to reward_rate_transition_duration_seconds), and
        // flattens out as seconds_since_genesis approaches
        // reward_rate_transition_duration_seconds.
        let s2 = s * s;

        // This looks backwards, but we think of variable rate as being added to
        // final growth rate, not initial, and the amount to add is up to
        // initial - final (where initial is thought of as being greater than
        // final).
        let dr = self.initial_reward_rate() - self.final_reward_rate();
        // variable_reward_rate varies from dr to 0 as round varies from
        // 1 to transition_round_count.
        let variable_reward_rate = s2 * dr;

        self.final_reward_rate() + variable_reward_rate
```

**File:** rs/sns/governance/src/reward.rs (L270-276)
```rust
    fn initial_reward_rate_basis_points_defects(&self) -> Vec<String> {
        require_field_set_and_in_range(
            "initial_reward_rate_basis_points",
            &self.initial_reward_rate_basis_points,
            ..Self::INITIAL_REWARD_RATE_BASIS_POINTS_CEILING,
        )
    }
```

**File:** rs/sns/init/src/lib.rs (L1175-1188)
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
```

**File:** rs/sns/init/src/gen/ic_sns_init.pb.v1.rs (L82-85)
```rust
    #[prost(uint64, optional, tag = "14")]
    pub initial_reward_rate_basis_points: ::core::option::Option<u64>,
    #[prost(uint64, optional, tag = "15")]
    pub final_reward_rate_basis_points: ::core::option::Option<u64>,
```
