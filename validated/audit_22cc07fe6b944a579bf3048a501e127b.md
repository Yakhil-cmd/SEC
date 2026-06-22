### Title
SNS Governance `VotingRewardsParameters` Update Applied Retroactively Without Settling Current Reward Period - (File: rs/sns/governance/src/governance.rs)

### Summary
`perform_manage_nervous_system_parameters` in the SNS Governance canister immediately overwrites `VotingRewardsParameters` (including `initial_reward_rate_basis_points`, `final_reward_rate_basis_points`, and `reward_rate_transition_duration_seconds`) without first settling the current in-progress reward period. The next call to `distribute_rewards` then applies the new rate retroactively to the entire unsettled interval since the last `RewardEvent`, mirroring the AutopoolFees bug class exactly.

### Finding Description
`perform_manage_nervous_system_parameters` is the execution handler for `ManageNervousSystemParameters` proposals:

```rust
// rs/sns/governance/src/governance.rs:2581-2597
fn perform_manage_nervous_system_parameters(
    &mut self,
    proposed_params: NervousSystemParameters,
) -> Result<(), GovernanceError> {
    let new_params = proposed_params.inherit_from(self.nervous_system_parameters_or_panic());
    match new_params.validate() {
        Ok(()) => {
            self.proto.parameters = Some(new_params);  // ← immediate overwrite, no checkpoint
            Ok(())
        }
        ...
    }
}
``` [1](#0-0) 

The reward calculation in `distribute_rewards` reads `voting_rewards_parameters` from the **current** (already-updated) state and uses it to price every round since the last `RewardEvent`:

```rust
// rs/sns/governance/src/governance.rs:5808-5871
let reward_start_timestamp_seconds = self.latest_reward_event()
    .end_timestamp_seconds.unwrap_or_default();
let new_rounds_count = now
    .saturating_sub(reward_start_timestamp_seconds)
    .saturating_div(round_duration_seconds);   // ← new round_duration_seconds

for i in 1..=new_rounds_count {
    let current_reward_rate = voting_rewards_parameters.reward_rate_at(...); // ← new rate
    result += current_reward_rate * voting_rewards_parameters.round_duration() * supply;
}
``` [2](#0-1) 

The proto definition itself acknowledges the hazard but provides no enforcement:

> "When this field is not populated, voting rewards are 'disabled'. **Once this is set, it probably should not be changed, because the results would probably be pretty confusing.**" [3](#0-2) 

`distribute_rewards` is driven by `run_periodic_tasks` / `should_distribute_rewards`, which fires only when `now - latest_reward_event.end_timestamp_seconds > round_duration_seconds`. There is no call to settle the current period inside `perform_manage_nervous_system_parameters`. [4](#0-3) 

### Impact Explanation
**Scenario — rate increase:**
1. SNS has been running for 20 days since the last `RewardEvent`. Old `final_reward_rate_basis_points` = 100 (1 %/yr).
2. A `ManageNervousSystemParameters` proposal passes, setting `final_reward_rate_basis_points` = 500 (5 %/yr).
3. `perform_manage_nervous_system_parameters` overwrites the parameters immediately.
4. The next `distribute_rewards` call prices all 20 days at 5 %/yr instead of 1 %/yr — a 5× inflation of the reward purse for that period.

**Scenario — rate decrease / round-duration increase:**
Conversely, increasing `round_duration_seconds` reduces `new_rounds_count`, causing the entire elapsed period to be priced at fewer, longer rounds, potentially under-rewarding voters who participated under the old schedule.

Both directions constitute incorrect token minting / maturity accounting, violating the conservation invariant of the SNS reward ledger.

### Likelihood Explanation
The `ManageNervousSystemParameters` action is a standard SNS governance proposal type reachable by any token holder with sufficient voting power. SNS projects frequently adjust tokenomics parameters post-launch. An SNS team that retains majority voting power (common in early-stage SNS deployments) can pass such a proposal unilaterally. Even without malicious intent, any legitimate rate-change proposal silently mis-prices the unsettled reward window. The proto comment acknowledges the hazard but does not prevent the action. [5](#0-4) 

### Recommendation
Before overwriting `voting_rewards_parameters` in `perform_manage_nervous_system_parameters`, the implementation should first settle the current reward period by calling `distribute_rewards` (or an equivalent checkpoint) so that all rounds elapsed under the old parameters are priced at the old rate. Only after the checkpoint should the new parameters take effect.

### Proof of Concept
1. Deploy an SNS with `initial_reward_rate_basis_points = 100`, `round_duration_seconds = 86400` (1 day).
2. Advance time by 10 days without triggering a reward event (e.g., no settled proposals, so rewards roll over).
3. Pass a `ManageNervousSystemParameters` proposal setting `initial_reward_rate_basis_points = 1000` (10×).
4. Advance time by 1 more day to trigger `should_distribute_rewards`.
5. Observe that `distribute_rewards` prices all 11 days at the new 10× rate instead of 10 days at 1× + 1 day at 10×.
6. The `total_available_e8s_equivalent` in the resulting `RewardEvent` will be ~10× larger than it should be for the first 10 days, inflating neuron maturity for all voters in that window. [6](#0-5)

### Citations

**File:** rs/sns/governance/src/governance.rs (L2581-2597)
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
```

**File:** rs/sns/governance/src/governance.rs (L5725-5753)
```rust
    fn should_distribute_rewards(&self) -> bool {
        let now = self.env.now();

        let voting_rewards_parameters = match &self
            .nervous_system_parameters_or_panic()
            .voting_rewards_parameters
        {
            None => return false,
            Some(ok) => ok,
        };
        let seconds_since_last_reward_event = now.saturating_sub(
            self.latest_reward_event()
                .end_timestamp_seconds
                .unwrap_or_default(),
        );

        let round_duration_seconds = match voting_rewards_parameters.round_duration_seconds {
            Some(s) => s,
            None => {
                log!(
                    ERROR,
                    "round_duration_seconds unset:\n{:#?}",
                    voting_rewards_parameters,
                );
                return false;
            }
        };

        seconds_since_last_reward_event > round_duration_seconds
```

**File:** rs/sns/governance/src/governance.rs (L5808-5875)
```rust
        let reward_start_timestamp_seconds = self
            .latest_reward_event()
            .end_timestamp_seconds
            .unwrap_or_default();
        let new_rounds_count = now
            .saturating_sub(reward_start_timestamp_seconds)
            .saturating_div(round_duration_seconds);
        if new_rounds_count == 0 {
            // This may happen, in case consider_distributing_rewards was called
            // several times at almost the same time. This is
            // harmless, just abandon.
            return;
        }

        let considered_proposals: Vec<ProposalId> =
            self.ready_to_be_settled_proposal_ids().collect();
        // RewardEvents are generated every time. If there are no proposals to reward, the rewards
        // purse is rolled over via the total_available_e8s_equivalent field.

        // Log if we are about to "backfill" rounds that were missed.
        if new_rounds_count > 1 {
            log!(
                INFO,
                "Some reward distribution should have happened, but were missed. \
                 It is now {}. Whereas, latest_reward_event:\n{:#?}",
                now,
                self.latest_reward_event(),
            );
        }
        let reward_event_end_timestamp_seconds = new_rounds_count
            .saturating_mul(round_duration_seconds)
            .saturating_add(reward_start_timestamp_seconds);

        // What's going on here looks a little complex, but it's just a slightly
        // more advanced version of simple (i.e. non-compounding) interest. The
        // main embellishment is because we are calculating the reward purse
        // over possibly more than one reward round. The possibility of multiple
        // rounds is why we loop over rounds. Otherwise, it boils down to the
        // simple interest formula:
        //
        //   principal * rate * duration
        //
        // Here, the entire token supply is used as the "principal", and the
        // length of a reward round is used as the duration. The reward rate
        // varies from round to round, and is calculated using
        // VotingRewardsParameters::reward_rate_at.
        let rewards_purse_e8s = {
            let mut result = Decimal::from(
                self.latest_reward_event()
                    .e8s_equivalent_to_be_rolled_over(),
            );
            let supply = i2d(supply.get_e8s());

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

            result
        };
```

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L1753-1757)
```rust
    /// When this field is not populated, voting rewards are "disabled". Once this
    /// is set, it probably should not be changed, because the results would
    /// probably be pretty confusing.
    #[prost(message, optional, tag = "19")]
    pub voting_rewards_parameters: ::core::option::Option<VotingRewardsParameters>,
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1228-1231)
```text
  // When this field is not populated, voting rewards are "disabled". Once this
  // is set, it probably should not be changed, because the results would
  // probably be pretty confusing.
  VotingRewardsParameters voting_rewards_parameters = 19;
```
