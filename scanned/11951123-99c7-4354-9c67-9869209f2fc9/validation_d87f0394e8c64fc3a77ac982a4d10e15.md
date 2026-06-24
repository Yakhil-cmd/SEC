### Title
`ManageNervousSystemParameters` Updates `voting_rewards_parameters` Without First Distributing Pending Rewards - (`File: rs/sns/governance/src/governance.rs`)

### Summary

`perform_manage_nervous_system_parameters` in SNS governance immediately overwrites `voting_rewards_parameters` (reward rate, round duration) without first settling the reward purse for the elapsed period. The next `distribute_rewards` call then applies the new parameters retroactively to the entire unaccrued window, giving neuron holders more or fewer rewards than they are entitled to for that window.

### Finding Description

`perform_manage_nervous_system_parameters` is the execution handler for `ManageNervousSystemParameters` proposals. It directly writes the new parameters into `self.proto.parameters` with no prior reward settlement: [1](#0-0) 

The reward distribution logic in `distribute_rewards` reads `voting_rewards_parameters` from the **current** (already-updated) state at the moment it runs: [2](#0-1) 

It then uses those parameters to compute the reward purse for **all rounds elapsed since the last reward event**, including rounds that occurred before the parameter change: [3](#0-2) 

The `round_duration_seconds` from the new parameters is also used to compute `new_rounds_count`, meaning a change to `round_duration_seconds` retroactively redefines how many rounds have elapsed: [4](#0-3) 

The `VotingRewardsParameters` struct holds all three mutable fields that affect reward magnitude: [5](#0-4) 

### Impact Explanation

When a governance proposal changes any of `initial_reward_rate_basis_points`, `final_reward_rate_basis_points`, or `round_duration_seconds`, the new values are applied retroactively to the entire unaccrued period on the next `distribute_rewards` invocation. Concretely:

- **Rate increase**: neuron holders receive a windfall for the pre-change period at the higher rate, inflating token supply beyond the intended schedule.
- **Rate decrease**: neuron holders are underpaid for the pre-change period, violating their earned entitlement.
- **`round_duration_seconds` decrease**: `new_rounds_count` grows, multiplying the reward purse for the elapsed window by the ratio of old/new duration, causing a large unintended payout.
- **`round_duration_seconds` increase**: `new_rounds_count` may drop to zero, silently discarding all pending rewards for the elapsed window.

This is a **governance reward accounting bug** with direct token-economic impact on all SNS neuron holders.

### Likelihood Explanation

`ManageNervousSystemParameters` is a standard, unprivileged governance action available to any SNS participant with sufficient voting power. SNS communities routinely adjust reward parameters over the lifetime of a project. The vulnerability fires automatically on the next periodic heartbeat after any such proposal executes — no special attacker knowledge or timing is required. The integration test `test_change_voting_rewards_round_duration` demonstrates that changing `round_duration_seconds` via proposal is an expected, tested workflow: [6](#0-5) 

### Recommendation

Call `consider_distributing_rewards` (or inline the equivalent settlement logic) at the beginning of `perform_manage_nervous_system_parameters`, before overwriting `self.proto.parameters`, so that the old parameters are used to settle all rounds elapsed up to the current moment. Only after that settlement should the new `voting_rewards_parameters` take effect.

```rust
fn perform_manage_nervous_system_parameters(
    &mut self,
    proposed_params: NervousSystemParameters,
) -> Result<(), GovernanceError> {
    // Settle rewards under the current parameters before changing them.
    self.consider_distributing_rewards();  // <-- add this

    let new_params = proposed_params.inherit_from(self.nervous_system_parameters_or_panic());
    // ... rest unchanged
}
```

### Proof of Concept

1. An SNS launches with `initial_reward_rate_basis_points = 200` (2 %) and `round_duration_seconds = 86400` (1 day).
2. After 12 hours (half a round), a `ManageNervousSystemParameters` proposal executes, doubling the rate to `initial_reward_rate_basis_points = 400`.
3. `perform_manage_nervous_system_parameters` writes the new parameters immediately with no reward settlement.
4. 12 hours later the next heartbeat fires `distribute_rewards`. It reads `initial_reward_rate_basis_points = 400` and computes the reward purse for the full 1-day elapsed window at 4 %, even though only the second half of that window should have earned 4 %. Neuron holders receive ~2× the intended reward for the first 12-hour sub-period.
5. Conversely, if the rate had been halved, neuron holders would be underpaid for the first 12 hours.

The root cause is the missing settlement call in `perform_manage_nervous_system_parameters` at: [7](#0-6)

### Citations

**File:** rs/sns/governance/src/governance.rs (L2581-2617)
```rust
    fn perform_manage_nervous_system_parameters(
        &mut self,
        proposed_params: NervousSystemParameters,
    ) -> Result<(), GovernanceError> {
        // Only set `self.proto.parameters` if "applying" the proposed params to the
        // current params results in valid params
        let new_params = proposed_params.inherit_from(self.nervous_system_parameters_or_panic());

        log!(
            INFO,
            "Setting Governance nervous system params to: {:?}",
            &new_params
        );

        match new_params.validate() {
            Ok(()) => {
                self.proto.parameters = Some(new_params);
                Ok(())
            }

            // Even though proposals are validated when they are first made, this is still
            // possible, because the inner value of a ManageNervousSystemParameters
            // proposal is only valid with respect to the current
            // nervous_system_parameters() at the time when the proposal was first
            // made. If nervous_system_parameters() changed (by another proposal) since
            // the current proposal was first made, the current proposal might have become
            // invalid. Basically, this might occur if there are conflicting (concurrent)
            // proposals, but we expect this to be highly unusual in practice.
            Err(msg) => Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "Failed to perform ManageNervousSystemParameters action, proposed \
                        parameters would lead to invalid NervousSystemParameters: {msg}"
                ),
            )),
        }
    }
```

**File:** rs/sns/governance/src/governance.rs (L5769-5782)
```rust
        let voting_rewards_parameters = match &self
            .nervous_system_parameters_or_panic()
            .voting_rewards_parameters
        {
            Some(voting_rewards_parameters) => voting_rewards_parameters,
            None => {
                log!(
                    ERROR,
                    "distribute_rewards called even though \
                     voting_rewards_parameters not set.",
                );
                return;
            }
        };
```

**File:** rs/sns/governance/src/governance.rs (L5812-5814)
```rust
        let new_rounds_count = now
            .saturating_sub(reward_start_timestamp_seconds)
            .saturating_div(round_duration_seconds);
```

**File:** rs/sns/governance/src/governance.rs (L5861-5872)
```rust
            for i in 1..=new_rounds_count {
                let seconds_since_genesis = round_duration_seconds
                    .saturating_mul(i)
                    .saturating_add(reward_start_timestamp_seconds)
                    .saturating_sub(self.proto.genesis_timestamp_seconds);

                let current_reward_rate = voting_rewards_parameters.reward_rate_at(
                    crate::reward::Instant::from_seconds_since_genesis(i2d(seconds_since_genesis)),
                );

                result += current_reward_rate * voting_rewards_parameters.round_duration() * supply;
            }
```

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L1817-1853)
```rust
pub struct VotingRewardsParameters {
    /// The amount of time between reward events.
    ///
    /// Must be > 0.
    ///
    /// During such periods, proposals enter the ReadyToSettle state. Once the round is over, voting
    /// for those proposals entitle voters to voting rewards. Such rewards are calculated in
    /// the governance canister's run_periodic_tasks function.
    ///
    /// This is a nominal amount. That is, the actual time between reward
    /// calculations and distribution cannot be guaranteed to be perfectly
    /// periodic, but actual inter-reward periods are generally expected to be
    /// within a few seconds of this.
    ///
    /// This supersedes super.reward_distribution_period_seconds.
    #[prost(uint64, optional, tag = "1")]
    pub round_duration_seconds: ::core::option::Option<u64>,
    /// The amount of time that the growth rate changes (presumably, decreases)
    /// from the initial growth rate to the final growth rate. (See the two
    /// *_reward_rate_basis_points fields bellow.) The transition is quadratic, and
    /// levels out at the end of the growth rate transition period.
    #[prost(uint64, optional, tag = "3")]
    pub reward_rate_transition_duration_seconds: ::core::option::Option<u64>,
    /// The amount of rewards is proportional to token_supply * current_rate. In
    /// turn, current_rate is somewhere between `initial_reward_rate_basis_points`
    /// and `final_reward_rate_basis_points`. In the first reward period, it is the
    /// initial growth rate, and after the growth rate transition period has elapsed,
    /// the growth rate becomes the final growth rate, and remains at that value for
    /// the rest of time. The transition between the initial and final growth rates is
    /// quadratic, and levels out at the end of the growth rate transition period.
    ///
    /// (A basis point is one in ten thousand.)
    #[prost(uint64, optional, tag = "4")]
    pub initial_reward_rate_basis_points: ::core::option::Option<u64>,
    #[prost(uint64, optional, tag = "5")]
    pub final_reward_rate_basis_points: ::core::option::Option<u64>,
}
```

**File:** rs/sns/integration_tests/src/proposals.rs (L1658-1670)
```rust
#[test]
fn test_change_voting_rewards_round_duration() {
    state_machine_test_on_sns_subnet(|runtime| async move {
        // Initialize the ledger with an account for a user who will make proposals
        let proposer = UserInfo::new(Sender::from_keypair(&TEST_USER1_KEYPAIR));
        // Initialize the ledger with an account for a user who will vote so we can control when
        // proposals are executed
        let voter = UserInfo::new(Sender::from_keypair(&TEST_USER2_KEYPAIR));
        let alloc = Tokens::from_tokens(1000).unwrap();

        let original_voting_rewards_round_duration_seconds =
            VOTING_REWARDS_PARAMETERS.round_duration_seconds.unwrap();
        let mut current_voting_rewards_round_duration_seconds =
```
