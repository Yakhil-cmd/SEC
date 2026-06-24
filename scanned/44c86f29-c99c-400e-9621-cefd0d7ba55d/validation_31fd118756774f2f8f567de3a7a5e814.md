### Title
`end_timestamp_seconds` Not Initialized in SNS `RewardEvent` Causes Unbounded Loop in `distribute_rewards` - (File: `rs/sns/governance/src/governance.rs`)

### Summary

The SNS governance canister's `distribute_rewards` function derives `reward_start_timestamp_seconds` from `self.latest_reward_event().end_timestamp_seconds.unwrap_or_default()`. Because `end_timestamp_seconds` is an `Option<u64>` protobuf field that defaults to `None`, `unwrap_or_default()` silently returns `0` whenever the field is absent. This causes `new_rounds_count` to be computed as `now / round_duration_seconds` — a value on the order of ~19,675 for a 1-day round at current Unix time — driving an unbounded loop that exhausts the canister's per-message instruction budget and permanently breaks SNS reward distribution.

### Finding Description

In `rs/sns/governance/src/governance.rs`, `distribute_rewards` computes the number of reward rounds to process:

```rust
let reward_start_timestamp_seconds = self
    .latest_reward_event()
    .end_timestamp_seconds
    .unwrap_or_default();          // ← returns 0 when field is None
let new_rounds_count = now
    .saturating_sub(reward_start_timestamp_seconds)
    .saturating_div(round_duration_seconds);
``` [1](#0-0) 

It then iterates over every missed round:

```rust
for i in 1..=new_rounds_count {
    ...
    let current_reward_rate = voting_rewards_parameters.reward_rate_at(...);
    result += current_reward_rate * voting_rewards_parameters.round_duration() * supply;
}
``` [2](#0-1) 

There is no cap on `new_rounds_count`. The only guard is `if new_rounds_count == 0 { return; }`, which does not fire when the field is absent. [3](#0-2) 

The `end_timestamp_seconds` field is declared `optional` in the SNS protobuf schema and maps to `Option<u64>` in Rust: [4](#0-3) 

The initialization guard only fires when `proto.latest_reward_event` is `None`:

```rust
if proto.latest_reward_event.is_none() {
    proto.latest_reward_event = Some(RewardEvent {
        end_timestamp_seconds: Some(now),   // ← only set on first init
        ...
    })
}
``` [5](#0-4) 

If the SNS governance proto is initialized with a `latest_reward_event` that is `Some(RewardEvent { end_timestamp_seconds: None, ... })` — which occurs for any SNS deployed before `end_timestamp_seconds` was added to the proto, or when a custom initial governance proto omits the field — the guard is bypassed and `end_timestamp_seconds` remains `None` permanently.

The same `unwrap_or_default()` pattern in `should_distribute_rewards` means the function always returns `true` when `end_timestamp_seconds` is `None`:

```rust
let seconds_since_last_reward_event = now.saturating_sub(
    self.latest_reward_event()
        .end_timestamp_seconds
        .unwrap_or_default(),   // ← 0 when None → seconds_since_last = now
);
``` [6](#0-5) 

This causes `distribute_rewards` to be invoked on every periodic task execution. [7](#0-6) 

### Impact Explanation

With `end_timestamp_seconds = None` and `round_duration_seconds = 86400` (1 day), `new_rounds_count ≈ 1,700,000,000 / 86400 ≈ 19,675`. The loop executes ~19,675 iterations of floating-point reward-rate arithmetic per `run_periodic_tasks` call. This exhausts the IC per-message instruction limit, trapping the canister message. Even if the loop completes, the accumulated `rewards_purse_e8s` overflows `u64`, triggering the early-return guard:

```rust
Err(err) => {
    log!(ERROR, "Looks like the rewards purse ({}) overflowed u64: ...");
    return;   // ← latest_reward_event is NOT updated
}
``` [8](#0-7) 

Because `latest_reward_event` is never updated, the next periodic task call recomputes the same enormous `new_rounds_count` and fails identically. The SNS governance canister's reward distribution is permanently broken: no neuron maturity is ever increased, and the canister wastes its instruction budget on every timer tick.

### Likelihood Explanation

Any SNS canister whose initial governance proto supplies a `latest_reward_event` with `end_timestamp_seconds` absent (the protobuf default) is affected. This includes SNS instances deployed before `end_timestamp_seconds` was introduced to the `RewardEvent` message. The periodic task is triggered automatically by the IC timer set in `init_timers` — no privileged access or special message is required. Any message to the SNS governance canister can advance the timer queue, making this reachable by any unprivileged user. [9](#0-8) 

### Recommendation

Replace `unwrap_or_default()` with an explicit fallback to `self.proto.genesis_timestamp_seconds` (analogous to the NNS fix of initializing `lastUpdatedDay` to the current block timestamp):

```rust
let reward_start_timestamp_seconds = self
    .latest_reward_event()
    .end_timestamp_seconds
    .unwrap_or(self.proto.genesis_timestamp_seconds);
```

Apply the same fix in `should_distribute_rewards`. Additionally, add a post-upgrade migration that sets `end_timestamp_seconds` to `genesis_timestamp_seconds` for any existing `RewardEvent` where the field is `None`, preventing the issue from manifesting on already-deployed SNS canisters.

### Proof of Concept

1. Deploy an SNS governance canister with an initial `GovernanceProto` that sets `latest_reward_event` to a non-`None` `RewardEvent` with `end_timestamp_seconds` absent (e.g., `RewardEvent { round: 0, ..Default::default() }`).
2. The initialization guard `if proto.latest_reward_event.is_none()` does not fire; `end_timestamp_seconds` remains `None`.
3. On the first `run_periodic_tasks` call, `should_distribute_rewards` evaluates `now.saturating_sub(0) > round_duration_seconds` → `true`.
4. `distribute_rewards` computes `new_rounds_count = now / round_duration_seconds`. For `now = 1_700_000_000` and `round_duration_seconds = 86400`, this is `≈ 19,675`.
5. The loop `for i in 1..=19675` runs, exhausting the instruction limit or overflowing `u64` in the reward purse calculation.
6. `latest_reward_event` is never updated; the next periodic task call repeats identically. Reward distribution is permanently broken.

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

**File:** rs/sns/governance/src/governance.rs (L5503-5521)
```rust
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

**File:** rs/sns/governance/src/governance.rs (L5735-5739)
```rust
        let seconds_since_last_reward_event = now.saturating_sub(
            self.latest_reward_event()
                .end_timestamp_seconds
                .unwrap_or_default(),
        );
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

**File:** rs/sns/governance/src/governance.rs (L5815-5820)
```rust
        if new_rounds_count == 0 {
            // This may happen, in case consider_distributing_rewards was called
            // several times at almost the same time. This is
            // harmless, just abandon.
            return;
        }
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

**File:** rs/sns/governance/src/governance.rs (L5878-5889)
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
```

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L1946-1947)
```rust
    #[prost(uint64, optional, tag = "5")]
    pub end_timestamp_seconds: ::core::option::Option<u64>,
```

**File:** rs/sns/governance/canister/canister.rs (L626-641)
```rust
fn init_timers() {
    governance_mut().proto.timers.replace(Timers {
        last_reset_timestamp_seconds: Some(now_seconds()),
        ..Default::default()
    });

    let new_timer_id = ic_cdk_timers::set_timer_interval(RUN_PERIODIC_TASKS_INTERVAL, async || {
        run_periodic_tasks().await
    });
    TIMER_ID.with(|saved_timer_id| {
        let mut saved_timer_id = saved_timer_id.borrow_mut();
        if let Some(saved_timer_id) = *saved_timer_id {
            ic_cdk_timers::clear_timer(saved_timer_id);
        }
        saved_timer_id.replace(new_timer_id);
    });
```
