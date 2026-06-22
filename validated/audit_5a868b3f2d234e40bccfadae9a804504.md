### Title
SNS Governance `perform_manage_nervous_system_parameters` Does Not Settle Current Reward Period Before Updating `voting_rewards_parameters` - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

When an SNS governance proposal changes `voting_rewards_parameters` (specifically `initial_reward_rate_basis_points`, `final_reward_rate_basis_points`, or `reward_rate_transition_duration_seconds`), the execution path does not first trigger a reward distribution to settle the already-elapsed portion of the current reward period. The next `distribute_rewards` call then retroactively applies the new rate parameters to all rounds since the last `RewardEvent`, including time that elapsed under the old parameters. This is the direct IC analog of M-04: a rate parameter is changed without first snapshotting the accrued state, causing retroactive reward recalculation.

---

### Finding Description

`perform_manage_nervous_system_parameters` in `rs/sns/governance/src/governance.rs` simply validates and overwrites `self.proto.parameters`:

```rust
fn perform_manage_nervous_system_parameters(
    &mut self,
    proposed_params: NervousSystemParameters,
) -> Result<(), GovernanceError> {
    let new_params = proposed_params.inherit_from(self.nervous_system_parameters_or_panic());
    match new_params.validate() {
        Ok(()) => {
            self.proto.parameters = Some(new_params);  // ← no reward settlement first
            Ok(())
        }
        ...
    }
}
``` [1](#0-0) 

The subsequent `distribute_rewards` call reads `voting_rewards_parameters` from the **current** (already-updated) parameters and uses them to compute the reward purse for **all rounds since the last `RewardEvent`**, including time that elapsed under the old parameters:

```rust
let voting_rewards_parameters = match &self
    .nervous_system_parameters_or_panic()
    .voting_rewards_parameters { ... };

let reward_start_timestamp_seconds = self.latest_reward_event()
    .end_timestamp_seconds.unwrap_or_default();
let new_rounds_count = now
    .saturating_sub(reward_start_timestamp_seconds)
    .saturating_div(round_duration_seconds);

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
``` [2](#0-1) 

The `reward_rate_at` function computes a quadratic interpolation between `initial_reward_rate_basis_points` and `final_reward_rate_basis_points` over `reward_rate_transition_duration_seconds`: [3](#0-2) 

Because `voting_rewards_parameters` is read from the live state at distribution time, any change to `initial_reward_rate_basis_points`, `final_reward_rate_basis_points`, or `reward_rate_transition_duration_seconds` retroactively alters the reward rate applied to the entire elapsed period since the last `RewardEvent`.

The `ManageNervousSystemParameters` action is a standard SNS proposal type, reachable by any neuron holder with sufficient voting power: [4](#0-3) 

---

### Impact Explanation

**Scenario — reward rate increase:**

1. Last `RewardEvent` at T0. `initial_reward_rate_basis_points = 100` (1%). Round duration = 7 days.
2. At T1 = T0 + 6 days, a `ManageNervousSystemParameters` proposal executes, raising `initial_reward_rate_basis_points` to 1000 (10%).
3. `should_distribute_rewards` returns false (6 days < 7 days), so no distribution yet.
4. At T2 = T0 + 7 days, `distribute_rewards` fires. It uses the new 10% rate for the entire 7-day period, not just the 1 day that elapsed under the new rate.
5. All stakers receive 10× the expected reward for the 6 days that elapsed under the old 1% rate.

**Scenario — reward rate decrease:**

The symmetric case: a rate decrease causes stakers to receive fewer rewards than they earned during the elapsed period, effectively confiscating accrued maturity.

**Scenario — `reward_rate_transition_duration_seconds` decrease:**

Decreasing this parameter causes `reward_rate_at` to return `final_reward_rate` (the lower rate) for time points that previously fell within the transition window, retroactively reducing rewards for the elapsed period. [5](#0-4) 

The impact is a ledger conservation bug: maturity minted to neurons does not correspond to the rate that was in effect when the time elapsed. An attacker who can pass a rate-increase proposal and stakes tokens just before execution receives a disproportionate share of the inflated reward pool for the current period.

---

### Likelihood Explanation

The attacker-controlled entry path is a `ManageNervousSystemParameters` proposal submitted via `manage_neuron` (an ingress update call), which is reachable by any unprivileged SNS neuron holder. No admin key or privileged role is required beyond holding enough voting power to pass the proposal. In many SNS deployments, token distribution is concentrated among a small number of early holders, making majority control realistic. The attack does not require frontrunning in the traditional sense — the attacker simply stakes tokens before the proposal's voting period ends (which is publicly visible on-chain). The attack is not repeatable indefinitely, but each governance parameter change creates a new opportunity.

---

### Recommendation

`perform_manage_nervous_system_parameters` should trigger a reward distribution to settle the current period before applying any change to `voting_rewards_parameters`. Concretely, if `voting_rewards_parameters` is being changed, call `distribute_rewards` (or an equivalent snapshot of the current accrued rewards) before overwriting `self.proto.parameters`. This ensures the reward rate change only applies to future periods, not to time already elapsed under the old parameters.

```rust
fn perform_manage_nervous_system_parameters(
    &mut self,
    proposed_params: NervousSystemParameters,
) -> Result<(), GovernanceError> {
    let new_params = proposed_params.inherit_from(self.nervous_system_parameters_or_panic());
    match new_params.validate() {
        Ok(()) => {
            // If voting_rewards_parameters is changing, settle the current period first.
            if proposed_params.voting_rewards_parameters.is_some() {
                // Trigger reward distribution with current parameters before the change.
                // (Requires access to current token supply; may need to be done
                //  asynchronously or via a cached supply value.)
                self.try_distribute_rewards_now();
            }
            self.proto.parameters = Some(new_params);
            Ok(())
        }
        ...
    }
}
```

---

### Proof of Concept

1. SNS is deployed with `initial_reward_rate_basis_points = 100` (1%), `final_reward_rate_basis_points = 50` (0.5%), `reward_rate_transition_duration_seconds = 8 years`, `round_duration_seconds = 604800` (7 days).
2. Last `RewardEvent` fires at T0. Attacker observes the state.
3. Attacker submits a `ManageNervousSystemParameters` proposal to set `initial_reward_rate_basis_points = 1000` (10%) and `final_reward_rate_basis_points = 500` (5%).
4. Attacker stakes additional tokens (increasing their neuron's voting power and maturity share) before the proposal's voting period ends.
5. Proposal passes and executes at T1 = T0 + 6 days via `perform_manage_nervous_system_parameters`, which writes the new parameters without settling rewards.
6. At T2 = T0 + 7 days, `run_periodic_tasks` calls `distribute_rewards`. It reads `initial_reward_rate_basis_points = 1000` from the current parameters and applies it to all 7 days since T0.
7. The reward pool for the period is 10× larger than it should have been for the first 6 days. The attacker's neuron, having been staked with extra tokens before the proposal executed, receives a disproportionate share of the inflated pool.
8. Existing stakers who did not add stake receive the same absolute maturity as before (their share is diluted by the attacker's extra stake), while the attacker captures the excess. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/sns/governance/src/governance.rs (L2144-2146)
```rust
            Action::ManageNervousSystemParameters(params) => {
                self.perform_manage_nervous_system_parameters(params)
            }
```

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

**File:** rs/sns/governance/src/reward.rs (L197-242)
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
    }
```
