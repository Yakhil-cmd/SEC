### Title
Silent Arithmetic Underflow in SNS Reward Round Epoch Calculation Inflates Voting Rewards - (File: rs/sns/governance/src/governance.rs)

### Summary
In the SNS governance `distribute_rewards` function, the per-round `seconds_since_genesis` calculation uses `saturating_sub` on `u64` values. When `reward_start_timestamp_seconds` is 0 (from an unset `end_timestamp_seconds` field in the latest `RewardEvent`), the subtraction of `genesis_timestamp_seconds` silently clamps to 0 for all early rounds. This causes `reward_rate_at` to be called with `seconds_since_genesis = 0` (genesis instant), returning the maximum initial reward rate instead of the correct rate, inflating the SNS voting rewards purse.

### Finding Description

In `rs/sns/governance/src/governance.rs`, the `distribute_rewards` function computes `seconds_since_genesis` for each reward round as follows:

```rust
let reward_start_timestamp_seconds = self
    .latest_reward_event()
    .end_timestamp_seconds
    .unwrap_or_default();   // ← returns 0 if field is None
``` [1](#0-0) 

Then, inside the reward loop:

```rust
for i in 1..=new_rounds_count {
    let seconds_since_genesis = round_duration_seconds
        .saturating_mul(i)
        .saturating_add(reward_start_timestamp_seconds)
        .saturating_sub(self.proto.genesis_timestamp_seconds);  // ← silent underflow
``` [2](#0-1) 

When `end_timestamp_seconds` is `None` in the stored `RewardEvent`, `reward_start_timestamp_seconds` becomes 0. For any SNS where `genesis_timestamp_seconds` is a real Unix epoch value (e.g., `1_700_000_000`), the expression `round_duration_seconds * i + 0 - genesis_timestamp_seconds` underflows for all rounds where `round_duration_seconds * i < genesis_timestamp_seconds`. The `saturating_sub` silently clamps the result to **0** instead of signaling an error.

The clamped value `0` is then passed to `reward_rate_at`:

```rust
let current_reward_rate = voting_rewards_parameters.reward_rate_at(
    crate::reward::Instant::from_seconds_since_genesis(i2d(seconds_since_genesis)),
);
``` [3](#0-2) 

Inside `reward_rate_at`, `seconds_since_genesis = 0` means `time_since_genesis = 0`, which is within the transition period, so the function returns the **initial (maximum) reward rate** rather than the correct rate for the actual elapsed time:

```rust
let dr = self.initial_reward_rate() - self.final_reward_rate();
let variable_reward_rate = s2 * dr;
self.final_reward_rate() + variable_reward_rate
``` [4](#0-3) 

The `end_timestamp_seconds` field in `RewardEvent` is `optional` in the proto definition and defaults to `None` for legacy SNS instances or during certain upgrade paths. The `Governance::new` constructor sets it to `now` for fresh deployments, but SNS instances that existed before this field was introduced will have `None` in their stored state. [5](#0-4) 

### Impact Explanation

Every reward distribution cycle for an affected SNS computes `rewards_purse_e8s` using the maximum initial reward rate instead of the correct time-adjusted rate. This causes the SNS governance canister to mint and distribute **more SNS tokens as voting rewards than the tokenomics schedule permits**. The excess minting dilutes the token supply beyond the intended schedule, constituting a ledger conservation violation. The `debug_assert` at line 5876 only checks that the purse is non-negative, not that it is within the correct bounds. [6](#0-5) 

### Likelihood Explanation

Any SNS instance whose `latest_reward_event.end_timestamp_seconds` is `None` (legacy state from before the field was added, or from an upgrade that did not migrate the field) is affected. The `distribute_rewards` function is called automatically by the periodic timer — no attacker action is required to trigger it. SNS token holders who vote receive inflated maturity, which they can later disburse as tokens. The impact scales with the number of affected SNS instances and the size of their token supplies.

### Recommendation

Replace the unchecked `saturating_sub` with an explicit guard that detects and rejects the underflow condition, analogous to the NNS governance check in `most_recent_fully_elapsed_reward_round_end_timestamp_seconds`:

```rust
let round_end_timestamp = round_duration_seconds
    .saturating_mul(i)
    .saturating_add(reward_start_timestamp_seconds);

if round_end_timestamp < self.proto.genesis_timestamp_seconds {
    log!(ERROR, "reward round end timestamp {} is before genesis {}; skipping",
         round_end_timestamp, self.proto.genesis_timestamp_seconds);
    return;
}
let seconds_since_genesis = round_end_timestamp - self.proto.genesis_timestamp_seconds;
```

Additionally, ensure that `end_timestamp_seconds` is always populated during SNS upgrades (migration of legacy `RewardEvent` records).

### Proof of Concept

1. Deploy an SNS with `genesis_timestamp_seconds = 1_700_000_000` and `round_duration_seconds = 604_800` (1 week).
2. Arrange for `latest_reward_event.end_timestamp_seconds` to be `None` (e.g., via a state migration that does not populate the field, or by examining a legacy SNS instance).
3. Advance time so that `now > reward_start_timestamp_seconds + round_duration_seconds` (i.e., at least one reward round has elapsed).
4. The periodic timer fires `distribute_rewards`. For round `i = 1`: `604_800 * 1 + 0 = 604_800`, then `604_800.saturating_sub(1_700_000_000) = 0`.
5. `reward_rate_at(Instant::from_seconds_since_genesis(0))` returns `initial_reward_rate` (e.g., 2% annualized) instead of the correct rate for the actual elapsed time.
6. The `rewards_purse_e8s` is inflated by the difference between the initial and correct rates, multiplied by the token supply, for every affected round.
7. Neurons that voted receive excess maturity, which can be disbursed as SNS tokens, minting beyond the intended supply schedule. [7](#0-6)

### Citations

**File:** rs/sns/governance/src/governance.rs (L726-742)
```rust
        if proto.latest_reward_event.is_none() {
            // Introduce a dummy reward event to mark the origin of the SNS instance era.
            // This is required to be able to compute accurately the rewards for the
            // very first reward distribution.
            proto.latest_reward_event = Some(RewardEvent {
                actual_timestamp_seconds: now,
                round: 0,
                settled_proposals: vec![],
                distributed_e8s_equivalent: 0,
                end_timestamp_seconds: Some(now),
                rounds_since_last_distribution: Some(0),
                // This value should be considered equivalent to None (allowing
                // the use of unwrap_or_default), but for consistency, we
                // explicitly initialize to 0.
                total_available_e8s_equivalent: Some(0),
            })
        }
```

**File:** rs/sns/governance/src/governance.rs (L5808-5811)
```rust
        let reward_start_timestamp_seconds = self
            .latest_reward_event()
            .end_timestamp_seconds
            .unwrap_or_default();
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

**File:** rs/sns/governance/src/governance.rs (L5876-5876)
```rust
        debug_assert!(rewards_purse_e8s >= dec!(0), "{}", rewards_purse_e8s);
```

**File:** rs/sns/governance/src/reward.rs (L236-241)
```rust
        let dr = self.initial_reward_rate() - self.final_reward_rate();
        // variable_reward_rate varies from dr to 0 as round varies from
        // 1 to transition_round_count.
        let variable_reward_rate = s2 * dr;

        self.final_reward_rate() + variable_reward_rate
```
