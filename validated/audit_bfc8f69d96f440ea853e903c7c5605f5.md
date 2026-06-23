### Title
`perform_manage_nervous_system_parameters` Updates `VotingRewardsParameters` Without First Checkpointing Rewards, Retroactively Altering Voting Rewards for Elapsed Time - (`File: rs/sns/governance/src/governance.rs`)

---

### Summary

In SNS Governance, executing a `ManageNervousSystemParameters` proposal that changes `VotingRewardsParameters` (e.g., `round_duration_seconds`, `initial_reward_rate_basis_points`, `final_reward_rate_basis_points`, or `reward_rate_transition_duration_seconds`) immediately overwrites the live parameters without first calling `distribute_rewards` to settle the reward period that has already elapsed. The next time `distribute_rewards` runs, it reads the **new** parameters to retroactively compute rewards for time that already passed under the **old** parameters, inflating or deflating neuron maturity for that period.

---

### Finding Description

`perform_manage_nervous_system_parameters` is the execution handler for `Action::ManageNervousSystemParameters` proposals in SNS Governance. It is called from `perform_action` with no prior reward settlement:

```rust
// rs/sns/governance/src/governance.rs ~L2144
Action::ManageNervousSystemParameters(params) => {
    self.perform_manage_nervous_system_parameters(params)
}
```

The handler simply validates and overwrites `self.proto.parameters`:

```rust
// rs/sns/governance/src/governance.rs ~L2597
self.proto.parameters = Some(new_params);
```

The periodic `distribute_rewards` function then reads the **current** (already-updated) `voting_rewards_parameters` to compute how many reward rounds have elapsed and at what rate:

```rust
// rs/sns/governance/src/governance.rs ~L5784
let round_duration_seconds = match voting_rewards_parameters.round_duration_seconds { ... };

// ~L5812
let new_rounds_count = now
    .saturating_sub(reward_start_timestamp_seconds)
    .saturating_div(round_duration_seconds);   // uses NEW value

// ~L5867
let current_reward_rate = voting_rewards_parameters.reward_rate_at(...); // uses NEW rate params
result += current_reward_rate * voting_rewards_parameters.round_duration() * supply;
```

Both the round-count calculation and the per-round reward rate are derived from the **post-change** parameters, applied retroactively to the time window `[last_reward_event.end_timestamp_seconds, now]`.

---

### Impact Explanation

**Scenario A – `round_duration_seconds` reduced (e.g., 7 days → 1 day)**

Suppose 5 days have elapsed since the last reward event under a 7-day round. Under the old parameters, `new_rounds_count = 5d / 7d = 0` (no distribution yet). After the parameter change, `new_rounds_count = 5d / 1d = 5`. The next `distribute_rewards` call distributes **5 rounds** of rewards for a period that was supposed to be mid-round, inflating neuron maturity.

**Scenario B – `initial_reward_rate_basis_points` or `final_reward_rate_basis_points` changed**

The `reward_rate_at` function uses the new basis-point values to compute the reward rate for every round in the elapsed window, retroactively increasing or decreasing the reward purse for time already passed.

In both cases, neuron maturity (the SNS equivalent of staking rewards) is incorrectly credited or debited for a time window that has already elapsed under different agreed-upon parameters.

---

### Likelihood Explanation

Any SNS token holder with sufficient voting power can submit a `ManageNervousSystemParameters` proposal to change `VotingRewardsParameters`. This is a routine governance action (e.g., an SNS community deciding to shorten reward rounds or adjust the reward rate schedule). The proposer need not be malicious; the retroactive effect is an unintended consequence of the missing checkpoint. The likelihood is **medium**: the action requires a governance majority, but it is a legitimate and expected governance operation.

---

### Recommendation

Call `distribute_rewards` (with the current token supply) at the start of `perform_manage_nervous_system_parameters`, before overwriting `self.proto.parameters`, to settle all elapsed reward rounds under the old parameters:

```diff
fn perform_manage_nervous_system_parameters(
    &mut self,
    proposed_params: NervousSystemParameters,
) -> Result<(), GovernanceError> {
+   // Settle any elapsed reward rounds under the current parameters
+   // before they are overwritten.
+   if let Some(supply) = self.get_current_token_supply() {
+       self.distribute_rewards(supply);
+   }
    let new_params = proposed_params.inherit_from(self.nervous_system_parameters_or_panic());
    ...
}
```

This mirrors the fix recommended in the external report: checkpoint the accumulator before mutating the parameters that drive it.

---

### Proof of Concept

1. An SNS is initialized with `round_duration_seconds = 604800` (7 days) and `initial_reward_rate_basis_points = 200` (2%).
2. 5 days pass. No reward event fires (correct: a full 7-day round has not elapsed).
3. A `ManageNervousSystemParameters` proposal passes, setting `round_duration_seconds = 86400` (1 day).
4. `perform_manage_nervous_system_parameters` overwrites `self.proto.parameters` immediately — no call to `distribute_rewards`.
5. On the next heartbeat, `distribute_rewards` runs:
   - `reward_start_timestamp_seconds` = end of last reward event (5 days ago)
   - `new_rounds_count = (5 * 86400) / 86400 = 5`
   - Five rounds of rewards are distributed for the 5-day window, each computed at the new rate.
6. Neurons receive 5× the intended reward for a period that was supposed to be a single partial round under the old 7-day schedule.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** rs/sns/governance/src/governance.rs (L2144-2146)
```rust
            Action::ManageNervousSystemParameters(params) => {
                self.perform_manage_nervous_system_parameters(params)
            }
```

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

**File:** rs/sns/governance/src/governance.rs (L5784-5814)
```rust
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
```

**File:** rs/sns/governance/src/governance.rs (L5861-5871)
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
```
