### Title
SNS Governance `distribute_rewards` Silently Drops Accumulated Voting Rewards Purse on `u64` Overflow, Permanently Losing Neuron Maturity - (`File: rs/sns/governance/src/governance.rs`)

### Summary

In SNS Governance, `distribute_rewards` computes a `rewards_purse_e8s` as a `Decimal` accumulating rolled-over rewards plus new rounds. If this value overflows `u64`, the function returns early **without updating `latest_reward_event`**. Because the rollover mechanism reads `total_available_e8s_equivalent` from the **previous** (stale) `latest_reward_event`, the accumulated purse is silently discarded on every subsequent call. A `ManageNervousSystemParameters` governance proposal that increases the reward rate or extends the round duration can push the purse into this overflow state, after which all accumulated voting rewards are permanently lost and proposals remain stuck in `ReadyToSettle` indefinitely.

### Finding Description

In `rs/sns/governance/src/governance.rs`, `distribute_rewards` computes the rewards purse as a `Decimal` and then attempts to convert it to `u64`:

```rust
let total_available_e8s_equivalent = Some(match u64::try_from(rewards_purse_e8s) {
    Ok(ok) => ok,
    Err(err) => {
        log!(ERROR, "Looks like the rewards purse ({}) overflowed u64: {}. \
             Therefore, we stop the current attempt to distribute voting rewards.",
             rewards_purse_e8s, err);
        return;   // <-- early return, no state update
    }
});
```

When this `return` fires:
1. `self.proto.latest_reward_event` is **not updated** — it retains its old `end_timestamp_seconds` and `total_available_e8s_equivalent`.
2. On the next call, `e8s_equivalent_to_be_rolled_over()` reads `total_available_e8s_equivalent` from the **stale** previous event (which was the last successfully committed value, not the overflowing one).
3. The new `rewards_purse_e8s` is computed again from that stale rollover value plus new rounds — it will overflow again, causing another early return.
4. This loop repeats indefinitely: the overflow state is self-perpetuating.
5. All proposals in `ReadyToSettle` state are never settled — their `reward_event_end_timestamp_seconds` is never set, their ballots are never cleared, and neuron maturity is never increased.

The overflow can be triggered by a legitimate `ManageNervousSystemParameters` proposal that increases `initial_reward_rate_basis_points` or `final_reward_rate_basis_points`, or by a very large token supply combined with many missed rounds. Once triggered, the system is permanently stuck.

The analog to the Euler report is exact: in Euler, `setInterestRateModel` during an overflow state resets the accumulator. Here, a `ManageNervousSystemParameters` proposal that increases the reward rate while the system is in the overflow state makes the overflow permanent (higher rate → larger purse → deeper overflow), and the accumulated rolled-over rewards are lost forever.

### Impact Explanation

- **Permanent loss of voting rewards (neuron maturity)**: All SNS neuron holders who voted on proposals in `ReadyToSettle` state receive zero maturity. The rewards purse — which can represent a significant fraction of the token supply — is silently discarded on every heartbeat.
- **Proposals permanently stuck in `ReadyToSettle`**: Ballots are never cleared, consuming memory indefinitely. The `max_number_of_proposals_with_ballots` limit can be reached, blocking new proposals.
- **Governance conservation bug**: The SNS token supply fraction allocated to voting rewards is computed but never credited to neurons, violating the economic invariant that voters receive rewards.

### Likelihood Explanation

The overflow requires `rewards_purse_e8s > u64::MAX` (≈ 1.844 × 10¹⁹ e8s = ~1.844 × 10¹¹ tokens). This is reachable in practice when:
- Many reward rounds are missed (e.g., the SNS governance canister is paused or the heartbeat fails for an extended period), causing `new_rounds_count` to be very large.
- A `ManageNervousSystemParameters` proposal sets a high `initial_reward_rate_basis_points` (up to the ceiling) combined with a large token supply.
- The rolled-over `total_available_e8s_equivalent` from previous rollover rounds accumulates.

The `ManageNervousSystemParameters` action is executable by any SNS with a passing governance proposal — no privileged access is required beyond normal SNS governance participation. The entry path is fully reachable by an unprivileged ledger/governance user.

### Recommendation

1. **Cap the purse at `u64::MAX` with saturation** instead of returning early, so the overflow does not block reward distribution:
   ```rust
   let total_available_e8s_equivalent = Some(
       u64::try_from(rewards_purse_e8s).unwrap_or(u64::MAX)
   );
   ```
2. **Alternatively**, update `latest_reward_event.end_timestamp_seconds` even on overflow so that time advances and the overflow state does not self-perpetuate.
3. **Add a guard** in `ManageNervousSystemParameters` validation that checks whether the new reward rate, combined with the current rolled-over purse and token supply, would cause an overflow.

### Proof of Concept

**Step 1**: Deploy an SNS with a large token supply (e.g., 10¹² tokens = 10²⁰ e8s) and a moderate initial reward rate.

**Step 2**: Cause many reward rounds to be missed (e.g., by pausing the governance canister's heartbeat for an extended period, or by submitting a `ManageNervousSystemParameters` proposal that sets `round_duration_seconds` to a very small value, causing `new_rounds_count` to be enormous on the next call).

**Step 3**: On the next `distribute_rewards` call, `rewards_purse_e8s` is computed as:
```
rolled_over + supply * rate * new_rounds_count
```
With `supply = 10²⁰ e8s`, `rate = 0.025/year ≈ 7.9e-10/second`, `round_duration = 1 day`, and `new_rounds_count = 10000` missed rounds:
```
rewards_purse ≈ 10²⁰ * 7.9e-10 * 86400 * 10000 ≈ 6.8e²⁰ > u64::MAX (1.84e¹⁹)
```

**Step 4**: The `u64::try_from` fails, the function returns early at line 5888, `latest_reward_event` is not updated.

**Step 5**: Submit a `ManageNervousSystemParameters` proposal increasing `initial_reward_rate_basis_points`. This executes successfully via `perform_manage_nervous_system_parameters` at line 2597, updating `self.proto.parameters`. Now the reward rate is higher, making the overflow worse on every subsequent call.

**Step 6**: All proposals in `ReadyToSettle` remain permanently stuck. Neuron maturity is never increased. The accumulated rewards purse is permanently lost. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** rs/sns/governance/src/governance.rs (L2579-2617)
```rust
    /// Executes a ManageNervousSystemParameters proposal by updating Governance's
    /// NervousSystemParameters
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

**File:** rs/sns/governance/src/governance.rs (L5720-5753)
```rust
    ///
    /// The end of the last reward round is recorded in self.latest_reward_event.
    ///
    /// The (current) length of a reward round is specified in
    /// self.nervous_system_parameters.voting_reward_parameters
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

**File:** rs/sns/governance/src/governance.rs (L5808-5814)
```rust
        let reward_start_timestamp_seconds = self
            .latest_reward_event()
            .end_timestamp_seconds
            .unwrap_or_default();
        let new_rounds_count = now
            .saturating_sub(reward_start_timestamp_seconds)
            .saturating_div(round_duration_seconds);
```

**File:** rs/sns/governance/src/governance.rs (L5854-5875)
```rust
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

**File:** rs/sns/governance/src/governance.rs (L5878-5890)
```rust
        let total_available_e8s_equivalent = Some(match u64::try_from(rewards_purse_e8s) {
            Ok(ok) => ok,
            Err(err) => {
                log!(
                    ERROR,
                    "Looks like the rewards purse ({}) overflowed u64: {}. \
                     Therefore, we stop the current attempt to distribute voting rewards.",
                    rewards_purse_e8s,
                    err,
                );
                return;
            }
        });
```

**File:** rs/sns/governance/src/governance.rs (L6083-6093)
```rust
        // Conclude this round of rewards.
        self.proto.latest_reward_event = Some(RewardEvent {
            round: new_reward_event_round,
            actual_timestamp_seconds: now,
            settled_proposals: considered_proposals,
            distributed_e8s_equivalent,
            end_timestamp_seconds: Some(reward_event_end_timestamp_seconds),
            rounds_since_last_distribution: Some(new_rounds_count),
            total_available_e8s_equivalent,
        })
    }
```

**File:** rs/sns/governance/src/types.rs (L2054-2060)
```rust
    pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
        if self.rewards_rolled_over() {
            self.total_available_e8s_equivalent.unwrap_or_default()
        } else {
            0
        }
    }
```
