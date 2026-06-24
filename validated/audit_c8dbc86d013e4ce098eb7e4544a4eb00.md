All cited code sections are confirmed in the repository. Let me verify one more detail about the `RewardEvent` proto definition.

Audit Report

## Title
Uninitialized `end_timestamp_seconds` in SNS `RewardEvent` Causes Massive Reward Inflation on Upgrade - (`rs/sns/governance/src/governance.rs`)

## Summary

`Governance::new()` only seeds a proper `end_timestamp_seconds: Some(now)` in `latest_reward_event` when the field is entirely `None`. Any SNS whose persisted proto already contains a `RewardEvent` with `end_timestamp_seconds: None` — the state of every SNS deployed before that optional field was introduced — passes the guard silently. On the next periodic-task heartbeat after upgrade, `distribute_rewards` treats Unix epoch 0 as the last reward boundary, computes ~20,000 elapsed rounds, and distributes a rewards purse that is several multiples of the entire token supply, permanently inflating it via the spawn-neuron maturity path.

## Finding Description

**Root cause — incomplete initialization guard in `Governance::new()`**

The guard at `rs/sns/governance/src/governance.rs` L726 is:

```rust
if proto.latest_reward_event.is_none() { … }
``` [1](#0-0) 

When the deserialized proto already holds `Some(RewardEvent { end_timestamp_seconds: None, … })` — the exact state of every SNS launched before `end_timestamp_seconds` (proto tag 5) was added as an `optional` field to replace the deprecated `round` (tag 1) — the condition is `false`, the block is skipped, and `end_timestamp_seconds` remains `None`. [2](#0-1) 

**`canister_post_upgrade` feeds the raw proto directly into `Governance::new()`** [3](#0-2) 

No migration or back-fill step exists between deserialization and `canister_init_()`.

**`should_distribute_rewards` fires immediately**

`end_timestamp_seconds.unwrap_or_default()` yields 0, so `seconds_since_last_reward_event ≈ now ≈ 1,750,000,000 s`, which far exceeds any `round_duration_seconds`. The function returns `true` on the first heartbeat. [4](#0-3) 

**`distribute_rewards` computes ~20,000 rounds**

`reward_start_timestamp_seconds = 0`, so:

```
new_rounds_count = now / round_duration_seconds
                 ≈ 1_750_000_000 / 86_400
                 ≈ 20 254   (daily-round SNS)
``` [5](#0-4) 

**Rewards purse loop accumulates 20,000+ rounds at maximum initial rate**

For early rounds where `round_duration_seconds * i + 0 < genesis_timestamp_seconds`, `saturating_sub` clamps `seconds_since_genesis` to 0, so `reward_rate_at(0)` returns the maximum initial rate for every one of the ~20,254 iterations. [6](#0-5) 

**Overflow guard does not protect against this**

The guard only aborts if the purse exceeds `u64::MAX`. For any SNS with a supply below ~33 billion tokens (in e8s), the purse fits in a `u64` and is distributed in full. [7](#0-6) 

## Impact Explanation

All neurons that voted on any settled proposal in the inflated reward round receive maturity proportional to the bloated purse. Maturity is convertible to governance tokens via the spawn-neuron path, permanently minting new tokens. With a 10%/year initial rate and daily rounds, the purse is approximately 5.55× the total supply, meaning the token supply can be inflated by several multiples in a single reward event. This constitutes **illegal minting and protocol insolvency** for the affected SNS, matching the High impact category: *"Significant SNS security impact with concrete user or protocol harm"* ($2,000–$10,000). For larger SNS instances with token supplies valued above $1M, the impact escalates to Critical.

## Likelihood Explanation

The precondition — a persisted `RewardEvent` with `end_timestamp_seconds: None` — is the exact state of every SNS deployed before the `optional uint64 end_timestamp_seconds = 5` field was introduced to replace the deprecated `round` field. No attacker action is required beyond the SNS being upgraded to the current codebase (a routine NNS-governed operation). The periodic-task timer fires automatically after upgrade; no user interaction is needed to trigger the inflated distribution. The condition is deterministic and repeatable on any qualifying SNS.

## Recommendation

In `Governance::new()`, extend the initialization guard to also back-fill `end_timestamp_seconds` when it is `None` inside an existing `latest_reward_event`:

```rust
if proto.latest_reward_event.is_none() {
    proto.latest_reward_event = Some(RewardEvent {
        end_timestamp_seconds: Some(now),
        // … other fields …
    });
} else if let Some(ref mut event) = proto.latest_reward_event {
    if event.end_timestamp_seconds.is_none() {
        // Back-fill for SNS instances deployed before this field existed.
        // Use genesis_timestamp_seconds as a safe lower bound.
        event.end_timestamp_seconds = Some(proto.genesis_timestamp_seconds);
    }
}
```

Alternatively, replace every `end_timestamp_seconds.unwrap_or_default()` call with `end_timestamp_seconds.unwrap_or(self.proto.genesis_timestamp_seconds)` so that a missing value is treated as genesis rather than epoch 0.

## Proof of Concept

1. Construct a `Governance` proto with `latest_reward_event: Some(RewardEvent { round: 1, end_timestamp_seconds: None, … })` (simulating any pre-field SNS state).
2. Call `Governance::new()` with this proto and a mock environment where `now ≈ 1_750_000_000`. Observe that the `is_none()` guard is `false` and `end_timestamp_seconds` remains `None`.
3. Call `should_distribute_rewards()`. Confirm it returns `true` because `now - 0 > round_duration_seconds`.
4. Call `distribute_rewards(supply)` with a representative token supply. Observe `new_rounds_count ≈ 20_254` and `rewards_purse_e8s ≈ 5.55 × supply_e8s`.
5. Confirm that voting neurons receive maturity proportional to the inflated purse, and that spawning those neurons would mint tokens far exceeding the original supply.

This is directly testable as a unit test in `rs/sns/governance/src/governance.rs` using the existing `MockEnvironment` and `NativeEnvironment` test infrastructure, with no mainnet interaction required.

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

**File:** rs/sns/governance/src/governance.rs (L5735-5753)
```rust
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

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1336-1383)
```text
  // DEPRECATED: Use end_timestamp_seconds instead.
  //
  // Rewards are (calculated and) distributed periodically in "rounds". Round 1
  // begins at start_time and ends at start_time + 1 * round_duration, where
  // start_time and round_duration are specified in VotingRewardsParameters.
  // Similarly, round 2 begins at the end of round number 1, and ends at
  // start_time + 2 * round_duration. Etc. There is no round 0.
  //
  // In the context of rewards, SNS start_time is analogous to NNS genesis time.
  //
  // On rare occasions, the reward event may cover several reward periods, when
  // it was not possible to process a reward event for a while. This means that
  // successive values in this field might not be consecutive, but they usually
  // are.
  uint64 round = 1;

  // Not to be confused with round_end_timestampe_seconds. This is just used to
  // record when the calculation (of voting rewards) was performed, not the time
  // range/events (i.e. proposals) that was operated on.
  uint64 actual_timestamp_seconds = 2;

  // The list of proposals that were taken into account during
  // this reward event.
  repeated ProposalId settled_proposals = 3;

  // The total amount of reward that was distributed during this reward event.
  //
  // The unit is "e8s equivalent" to insist that, while this quantity is on
  // the same scale as governance tokens, maturity is not directly convertible
  // to governance tokens: conversion requires a minting event.
  uint64 distributed_e8s_equivalent = 4;

  // All proposals that were "ready to settle" up to this time were
  // considered.
  //
  // If a proposal is "ready to settle", it simply means that votes are no
  // longer accepted (votes can still be accepted for reward purposes after the
  // proposal is decided), but rewards have not yet been given yet (on account
  // of the proposal).
  //
  // The reason this should be used instead of `round` is that the duration of a
  // round can be changed via proposal. Such changes cause round numbers to be
  // not comparable without also knowing the associated round duration.
  //
  // Being able to change round duration does not exist in NNS (yet), and there
  // is (currently) no intention to add that feature, but it could be done by
  // making similar changes.
  optional uint64 end_timestamp_seconds = 5;
```

**File:** rs/sns/governance/canister/canister.rs (L272-290)
```rust
#[post_upgrade]
fn canister_post_upgrade() {
    log!(INFO, "Executing post upgrade");

    let governance_proto = with_upgrades_memory(|memory| {
        let result: Result<sns_gov_pb::Governance, _> = load_protobuf(memory);
        result
    })
    .expect(
        "Error deserializing canister state post-upgrade with MemoryManager memory segment. \
             CANISTER MIGHT HAVE BROKEN STATE!!!!.",
    );

    canister_init_(governance_proto);

    init_timers();

    log!(INFO, "Completed post upgrade");
}
```
