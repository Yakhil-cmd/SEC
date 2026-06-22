### Title
SNS Governance `perform_manage_nervous_system_parameters` Updates Reward Rate Without First Distributing Pending Rewards, Causing Neuron Maturity Loss - (File: rs/sns/governance/src/governance.rs)

### Summary

The SNS governance canister's `perform_manage_nervous_system_parameters` function applies new `VotingRewardsParameters` (including `initial_reward_rate_basis_points`, `final_reward_rate_basis_points`, and `round_duration_seconds`) immediately without first distributing pending voting rewards. Because `run_periodic_tasks` calls `process_proposals` (which executes the parameter change) **before** calling `distribute_rewards`, any reduction in the reward rate retroactively applies to the already-elapsed but not-yet-settled reward period, causing neuron owners to lose maturity they had already earned.

### Finding Description

In `rs/sns/governance/src/governance.rs`, `run_periodic_tasks` (line 5472) executes in this order:

1. `self.process_proposals()` — which calls `perform_manage_nervous_system_parameters` for any decided `ManageNervousSystemParameters` proposal, immediately overwriting `self.proto.parameters` with the new `VotingRewardsParameters`.
2. `if should_distribute_rewards { self.distribute_rewards(supply) }` — which reads the **already-updated** `voting_rewards_parameters` to compute the reward purse for all elapsed rounds since the last reward event. [1](#0-0) 

`perform_manage_nervous_system_parameters` performs no flush of pending rewards before overwriting the parameters: [2](#0-1) 

`distribute_rewards` then uses the **current** (already-changed) `voting_rewards_parameters` to calculate the reward purse for every round since the last reward event: [3](#0-2) 

The reward rate is computed via `reward_rate_at`, which reads `initial_reward_rate_basis_points` and `final_reward_rate_basis_points` from the live parameters: [4](#0-3) 

The codebase itself acknowledges this hazard in the proto comment for `voting_rewards_parameters`:

> "Once this is set, it probably should not be changed, because the results would probably be pretty confusing." [5](#0-4) 

### Impact Explanation

When a `ManageNervousSystemParameters` proposal reduces `initial_reward_rate_basis_points` or `final_reward_rate_basis_points` (e.g., from 200 bps to 0), the next invocation of `distribute_rewards` calculates the reward purse for the **entire elapsed period** at the new lower rate. Neurons that voted during that period under the old higher rate receive proportionally less maturity — or zero maturity if the rate is set to 0. This is a direct, irreversible loss of `maturity_e8s_equivalent` for all participating neuron owners.

### Likelihood Explanation

Any SNS community that legitimately decides to reduce its voting reward rate (a common tokenomics action) will trigger this bug without any malicious intent. The `ManageNervousSystemParameters` proposal type is the standard, documented mechanism for changing `VotingRewardsParameters`. The vulnerability fires automatically in the same `run_periodic_tasks` tick that executes the proposal, or in the very next tick after the reward round ends — whichever comes first. No special timing or coordination is required beyond passing the proposal. [6](#0-5) 

### Recommendation

Before applying new `VotingRewardsParameters` in `perform_manage_nervous_system_parameters`, call `distribute_rewards` (or an equivalent flush) so that all rewards accrued under the old parameters are settled first. This mirrors the fix recommended in the original report: "Update all the pools before setting a new reward per second value."

Concretely, inside `perform_manage_nervous_system_parameters`, before `self.proto.parameters = Some(new_params)`, the function should check `should_distribute_rewards()` and, if true, fetch the token supply and call `distribute_rewards(supply)` synchronously (or mark a flag that forces distribution before the parameter swap takes effect).

### Proof of Concept

1. An SNS is deployed with `initial_reward_rate_basis_points = 200` (2 %) and `final_reward_rate_basis_points = 100` (1 %).
2. Neurons vote on proposals during reward round N. The round has not yet ended.
3. A `ManageNervousSystemParameters` proposal is passed that sets both basis-point fields to `0`.
4. On the next `run_periodic_tasks` tick after the round ends:
   - `process_proposals()` executes the proposal → `perform_manage_nervous_system_parameters` sets `self.proto.parameters` to the new params (0 % rate). No `distribute_rewards` call is made.
   - `should_distribute_rewards()` returns `true` (round N has elapsed).
   - `distribute_rewards(supply)` is called. It reads `voting_rewards_parameters` — now 0 % — and computes `rewards_purse_e8s = 0` for round N.
5. All neurons that voted during round N receive **zero** maturity increment, despite having participated under the 2 % rate.

The reward purse calculation that uses the already-overwritten rate: [7](#0-6)

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

**File:** rs/sns/governance/src/governance.rs (L5472-5521)
```rust
    pub async fn run_periodic_tasks(&mut self) {
        use ic_cdk::println;

        self.process_proposals();

        // None of the upgrade-related tasks should interleave with one another or themselves, so we acquire a global
        // lock for the duration of their execution. This will return `false` if the lock has already been acquired less
        // than 10 minutes ago by a previous invocation of `run_periodic_tasks`, in which case we skip the
        // upgrade-related tasks.
        if self.acquire_upgrade_periodic_task_lock() {
            // We only want to check the upgrade status if we are currently executing an upgrade.
            if self.should_check_upgrade_status() {
                self.check_upgrade_status().await;
            }

            if self.should_refresh_cached_upgrade_steps() {
                match self.try_temporarily_lock_refresh_cached_upgrade_steps() {
                    Err(err) => {
                        log!(ERROR, "{}", err);
                    }
                    Ok(deployed_version) => {
                        self.refresh_cached_upgrade_steps(deployed_version).await;
                    }
                }
            }

            self.initiate_upgrade_if_sns_behind_target_version().await;

            self.release_upgrade_periodic_task_lock();
        }

        let should_distribute_rewards = self.should_distribute_rewards();

        // Getting the total governance token supply from the ledger is expensive enough
        // that we don't want to do it on every call to `run_periodic_tasks`. So
        // we only fetch it when it's needed, which is when rewards should be
        // distributed
        if should_distribute_rewards {
            match self.ledger.total_supply().await {
                Ok(supply) => {
                    // Distribute rewards
                    self.distribute_rewards(supply);
                }
                Err(e) => log!(
                    ERROR,
                    "Error when getting total governance token supply: {}",
                    GovernanceError::from(e)
                ),
            }
        }
```

**File:** rs/sns/governance/src/governance.rs (L5769-5871)
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

        let round_duration_seconds = match voting_rewards_parameters.round_duration_seconds {
            Some(s) => s,
            None => {
                log!(
                    ERROR,
                    "round_duration_seconds not set:\n{:#?}",
                    voting_rewards_parameters,
                );
                return;
            }
        };
        // This guard is needed, because we'll divide by this amount shortly.
        if round_duration_seconds == 0 {
            // This is important, but emitting this every time will be spammy, because this gets
            // called during run_periodic_tasks.
            log!(
                ERROR,
                "round_duration_seconds ({}) is not positive. \
                 Therefore, we cannot calculate voting rewards.",
                round_duration_seconds,
            );
            return;
        }

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

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L1753-1757)
```rust
    /// When this field is not populated, voting rewards are "disabled". Once this
    /// is set, it probably should not be changed, because the results would
    /// probably be pretty confusing.
    #[prost(message, optional, tag = "19")]
    pub voting_rewards_parameters: ::core::option::Option<VotingRewardsParameters>,
```
