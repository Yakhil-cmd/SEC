### Title
SNS Governance `ManageNervousSystemParameters` Updates `round_duration_seconds` Without First Settling Pending Rewards - (`rs/sns/governance/src/governance.rs`)

---

### Summary

When an SNS governance proposal of type `ManageNervousSystemParameters` is executed to change `voting_rewards_parameters.round_duration_seconds`, the function `perform_manage_nervous_system_parameters` immediately overwrites the stored parameters without first calling `distribute_rewards` (or `consider_distributing_rewards`) to settle the reward period that elapsed under the old duration. The subsequent reward distribution then uses the new `round_duration_seconds` to retroactively re-partition the already-elapsed time, producing incorrect reward amounts for SNS neuron holders.

---

### Finding Description

`perform_manage_nervous_system_parameters` in `rs/sns/governance/src/governance.rs` simply validates and stores the new parameters:

```rust
fn perform_manage_nervous_system_parameters(
    &mut self,
    proposed_params: NervousSystemParameters,
) -> Result<(), GovernanceError> {
    let new_params = proposed_params.inherit_from(self.nervous_system_parameters_or_panic());
    match new_params.validate() {
        Ok(()) => {
            self.proto.parameters = Some(new_params);  // ← parameters overwritten immediately
            Ok(())
        }
        ...
    }
}
``` [1](#0-0) 

No reward settlement is triggered before the overwrite. The next periodic call to `should_distribute_rewards` then reads the **new** `round_duration_seconds` and compares it against the time elapsed since the last reward event:

```rust
let seconds_since_last_reward_event = now.saturating_sub(
    self.latest_reward_event().end_timestamp_seconds.unwrap_or_default(),
);
...
seconds_since_last_reward_event > round_duration_seconds
``` [2](#0-1) 

If `round_duration_seconds` is **decreased** (e.g., from 7 days to 1 day), this condition can become immediately true even though the elapsed time was accumulated under the old, longer duration. The subsequent `distribute_rewards` call then computes `new_rounds_count` and `reward_event_end_timestamp_seconds` using the new (shorter) duration:

```rust
let new_rounds_count = now
    .saturating_sub(reward_start_timestamp_seconds)
    .saturating_div(round_duration_seconds);   // ← uses new duration
...
let reward_event_end_timestamp_seconds = new_rounds_count
    .saturating_mul(round_duration_seconds)    // ← uses new duration
    .saturating_add(reward_start_timestamp_seconds);
``` [3](#0-2) 

The reward purse for each of those retroactively-created rounds is then calculated using `reward_rate_at` with `seconds_since_genesis` derived from the new `round_duration_seconds`:

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
``` [4](#0-3) 

The `round_duration()` used in the reward purse multiplication is also sourced from the newly-updated parameters, meaning the entire reward calculation for the elapsed period is performed with the wrong time granularity. [5](#0-4) 

---

### Impact Explanation

When `round_duration_seconds` is decreased via a legitimate governance proposal:

1. The `should_distribute_rewards` gate fires immediately on the next periodic task, even though the elapsed time was accumulated under the old (longer) duration.
2. `new_rounds_count` is inflated (e.g., 3 days elapsed / 1 day new duration = 3 rounds, instead of the correct 0 rounds under the old 7-day duration).
3. Each of those 3 synthetic rounds uses the new `round_duration_seconds` to compute `seconds_since_genesis`, shifting the position on the reward-rate transition curve and producing incorrect per-round reward rates.
4. The total maturity minted to neuron holders is therefore incorrect — either over-distributed or under-distributed — violating the SNS token economics invariant.

Conversely, if `round_duration_seconds` is **increased**, the next distribution is delayed and proposals that entered `ReadyToSettle` under the old duration are held in limbo longer than voters expected, with their reward purse calculated at the wrong rate.

---

### Likelihood Explanation

Any SNS community can submit a `ManageNervousSystemParameters` proposal to change `round_duration_seconds`. The integration test `test_change_voting_rewards_round_duration` in `rs/sns/integration_tests/src/proposals.rs` explicitly exercises this path, confirming it is a supported and expected governance action. [6](#0-5) 

The bug manifests on every such legitimate parameter change — no malicious intent is required. The entry path is: SNS governance participant submits proposal → community votes yes → `perform_manage_nervous_system_parameters` executes → next `run_periodic_tasks` tick triggers incorrect reward distribution.

---

### Recommendation

Before overwriting `self.proto.parameters` in `perform_manage_nervous_system_parameters`, call `consider_distributing_rewards` (or the synchronous `distribute_rewards` path) to settle any reward period that has elapsed under the current `round_duration_seconds`. This ensures the old duration is used for the time that actually elapsed under it, and the new duration only applies to future periods — directly analogous to calling `execute_epoch_operations` before a config update in the referenced Anchor report.

---

### Proof of Concept

1. Deploy an SNS with `round_duration_seconds = 604800` (7 days).
2. Advance time by 3 days. No reward distribution fires (3 days < 7 days).
3. Submit and pass a `ManageNervousSystemParameters` proposal setting `round_duration_seconds = 86400` (1 day).
4. `perform_manage_nervous_system_parameters` stores the new parameters immediately with no reward settlement.
5. On the next `run_periodic_tasks` tick, `should_distribute_rewards` evaluates `3 days > 1 day` → `true`.
6. `distribute_rewards` computes `new_rounds_count = 3 days / 1 day = 3` and mints rewards for 3 synthetic 1-day rounds, each at a reward rate computed at the wrong position on the transition curve.
7. The actual correct behavior would be: 0 rounds distributed (3 days elapsed < 7-day old duration), with the 3-day partial period rolled over to the next event under the new 1-day duration. [1](#0-0) [7](#0-6) [8](#0-7)

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

**File:** rs/sns/governance/src/governance.rs (L5719-5754)
```rust
    /// Returns `true` if enough time has passed since the end of the last reward round.
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

**File:** rs/sns/governance/src/reward.rs (L85-95)
```rust
impl Duration {
    pub fn from_secs(seconds: Decimal) -> Self {
        Self {
            days: seconds * *DAYS_PER_SECOND,
        }
    }

    pub fn as_secs(&self) -> Decimal {
        self.days * *ONE_DAY_SECONDS
    }
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
