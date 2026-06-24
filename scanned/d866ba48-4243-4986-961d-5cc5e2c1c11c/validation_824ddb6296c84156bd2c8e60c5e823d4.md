### Title
Incorrect Default for `end_timestamp_seconds` in SNS Governance Reward Period Boundary Causes Massive Over-Distribution of Maturity - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS governance `distribute_rewards` and `should_distribute_rewards` functions use `end_timestamp_seconds.unwrap_or_default()` to determine the start of the current reward period. When `end_timestamp_seconds` is `None` (the initial/genesis `RewardEvent` state), `unwrap_or_default()` silently returns `0` — the Unix epoch — instead of the SNS genesis timestamp. This causes the first reward distribution to count all time elapsed since January 1, 1970, rather than since SNS genesis, leading to massive over-distribution of neuron maturity.

---

### Finding Description

In `rs/sns/governance/src/governance.rs`, `should_distribute_rewards` computes:

```rust
let seconds_since_last_reward_event = now.saturating_sub(
    self.latest_reward_event()
        .end_timestamp_seconds
        .unwrap_or_default(),   // ← returns 0 when None
);
seconds_since_last_reward_event > round_duration_seconds
``` [1](#0-0) 

When `end_timestamp_seconds` is `None` (the default for the genesis `RewardEvent`), `unwrap_or_default()` returns `0`, so `seconds_since_last_reward_event = now` (a Unix timestamp of ~1.7 billion seconds). This is always greater than `round_duration_seconds`, so `should_distribute_rewards` returns `true` immediately after SNS creation.

Then in `distribute_rewards`, the same pattern is used to anchor the reward period:

```rust
let reward_start_timestamp_seconds = self
    .latest_reward_event()
    .end_timestamp_seconds
    .unwrap_or_default();   // ← 0 when None
let new_rounds_count = now
    .saturating_sub(reward_start_timestamp_seconds)   // now - 0 = now
    .saturating_div(round_duration_seconds);           // now / round_duration ≈ 19,675 rounds
``` [2](#0-1) 

With `reward_start_timestamp_seconds = 0`, the loop that builds the rewards purse iterates for `~19,675` rounds (for a 1-day round duration and a current Unix timestamp):

```rust
for i in 1..=new_rounds_count {
    let seconds_since_genesis = round_duration_seconds
        .saturating_mul(i)
        .saturating_add(reward_start_timestamp_seconds)   // 0
        .saturating_sub(self.proto.genesis_timestamp_seconds);  // underflows → 0
    // reward rate is evaluated at genesis (initial/maximum rate) for all rounds
    result += current_reward_rate * voting_rewards_parameters.round_duration() * supply;
}
``` [3](#0-2) 

Because `reward_start_timestamp_seconds = 0` and `self.proto.genesis_timestamp_seconds` is a large Unix timestamp, the `saturating_sub` underflows to `0` for nearly all `i`, meaning the reward rate is evaluated at genesis (the maximum initial rate) for all ~19,675 rounds. The total maturity minted is approximately:

```
~19,675 × initial_reward_rate × round_duration × total_supply
```

For a 10%/year initial rate, this yields ~5,390% of total supply distributed as maturity in a single event.

The `RewardEvent` proto field `end_timestamp_seconds` is `Option<u64>` and is `None` for the genesis event: [4](#0-3) 

The NNS governance avoids this class of bug entirely by using an integer `day_after_genesis` counter anchored to `genesis_timestamp_seconds`, never relying on an `Option` timestamp that defaults to the Unix epoch: [5](#0-4) 

---

### Impact Explanation

Any SNS neuron holder receives a proportional share of the over-distributed maturity. On the first heartbeat after SNS launch, the governance canister mints maturity equivalent to thousands of reward rounds instead of one. This can:

- Drain the SNS token supply through maturity spawning
- Permanently distort neuron voting power and token economics
- Undermine trust in the SNS governance system

---

### Likelihood Explanation

This triggers automatically on the first execution of `run_periodic_tasks` after SNS creation, with no privileged action required. Any SNS neuron holder benefits. The entry path is the governance canister's own periodic heartbeat — fully reachable by any participant who holds neurons in the SNS.

---

### Recommendation

Replace `unwrap_or_default()` (which silently falls back to Unix epoch `0`) with an explicit fallback to the SNS genesis timestamp:

```rust
let reward_start_timestamp_seconds = self
    .latest_reward_event()
    .end_timestamp_seconds
    .unwrap_or(self.proto.genesis_timestamp_seconds);  // ← correct boundary
```

Apply the same fix in `should_distribute_rewards`. This mirrors the NNS approach of anchoring all period calculations to genesis.

---

### Proof of Concept

1. Deploy a new SNS with `round_duration_seconds = 86400` (1 day) and `initial_reward_rate_basis_points = 1000` (10%/year).
2. The genesis `RewardEvent` has `end_timestamp_seconds = None`.
3. On the first heartbeat, `should_distribute_rewards` returns `true` because `now - 0 > 86400`.
4. `distribute_rewards` computes `new_rounds_count = now / 86400 ≈ 19,675`.
5. The rewards loop runs 19,675 iterations, each at the initial reward rate (since `seconds_since_genesis` underflows to 0 for all rounds).
6. Total maturity distributed ≈ `19,675 × (10%/365) × supply ≈ 5,390% × supply`.
7. All SNS neuron holders receive proportional maturity windfalls; the SNS treasury is effectively drained. [6](#0-5) [7](#0-6)

### Citations

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

**File:** rs/sns/governance/src/governance.rs (L5808-5839)
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

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L1946-1947)
```rust
    #[prost(uint64, optional, tag = "5")]
    pub end_timestamp_seconds: ::core::option::Option<u64>,
```

**File:** rs/nns/governance/src/governance.rs (L6612-6615)
```rust
        let day_after_genesis =
            (now - self.heap_data.genesis_timestamp_seconds) / REWARD_DISTRIBUTION_PERIOD_SECONDS;
        let last_event_day_after_genesis = latest_reward_event.day_after_genesis;
        let days = last_event_day_after_genesis..day_after_genesis;
```
