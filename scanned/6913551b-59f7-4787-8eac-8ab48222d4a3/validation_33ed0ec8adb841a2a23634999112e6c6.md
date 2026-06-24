### Title
Incorrect Reward Period Baseline via `end_timestamp_seconds.unwrap_or_default()` Causes Massive SNS Governance Token Over-Distribution - (File: `rs/sns/governance/src/governance.rs`)

### Summary
In the SNS governance canister's `distribute_rewards` function, the baseline timestamp for reward round calculation is derived from `latest_reward_event().end_timestamp_seconds.unwrap_or_default()`. When `end_timestamp_seconds` is `None` — which occurs for any SNS canister whose stored `latest_reward_event` predates the introduction of this optional field — the baseline silently collapses to Unix epoch 0. This causes `new_rounds_count` to be computed as `now / round_duration_seconds` (potentially ~19,675 rounds for a 1-day SNS), and the per-round `seconds_since_genesis` to saturate to 0 via unsigned underflow for nearly all iterations, applying the maximum genesis-era reward rate across all phantom rounds. The result is a one-time, automatic, massive over-distribution of governance tokens on the first periodic task execution after upgrade.

### Finding Description

In `rs/sns/governance/src/governance.rs`, `distribute_rewards` computes the reward baseline as:

```rust
let reward_start_timestamp_seconds = self
    .latest_reward_event()
    .end_timestamp_seconds
    .unwrap_or_default();   // returns 0 if None
``` [1](#0-0) 

`new_rounds_count` is then:

```rust
let new_rounds_count = now
    .saturating_sub(reward_start_timestamp_seconds)
    .saturating_div(round_duration_seconds);
``` [2](#0-1) 

When `end_timestamp_seconds` is `None`, `reward_start_timestamp_seconds = 0`, so `new_rounds_count = now / round_duration_seconds`. For a 1-day round SNS at Unix time ~1,700,000,000, this yields ~19,675 phantom rounds.

Inside the reward purse loop, `seconds_since_genesis` is:

```rust
let seconds_since_genesis = round_duration_seconds
    .saturating_mul(i)
    .saturating_add(reward_start_timestamp_seconds)
    .saturating_sub(self.proto.genesis_timestamp_seconds);
``` [3](#0-2) 

With `reward_start_timestamp_seconds = 0` and `genesis_timestamp_seconds ≈ 1,700,000,000`, the expression `round_duration_seconds * i + 0 - genesis_timestamp_seconds` underflows (u64 saturating_sub) to 0 for all ~19,675 iterations. The `reward_rate_at` call therefore uses the maximum genesis-era rate for every phantom round. [4](#0-3) 

The `should_distribute_rewards` guard has the same flaw — it also calls `end_timestamp_seconds.unwrap_or_default()`, so it returns `true` immediately when the field is `None`, unconditionally triggering `distribute_rewards`. [5](#0-4) 

The genesis-era initialization only sets `end_timestamp_seconds` when `latest_reward_event` is entirely absent. If an SNS was upgraded from a version that stored a `RewardEvent` without this field, the existing event is preserved with `end_timestamp_seconds: None`, and the initialization guard is skipped. [6](#0-5) 

The `end_timestamp_seconds` field is declared `optional` in the proto, confirming it can be absent in stored state. [7](#0-6) 

### Impact Explanation

On the first periodic task execution after an SNS upgrade where `end_timestamp_seconds` is `None`:

1. `new_rounds_count ≈ 19,675` phantom rounds are computed.
2. All 19,675 iterations use the maximum genesis reward rate (due to u64 underflow to 0).
3. The rewards purse is inflated by a factor of ~19,675 relative to a normal single-round distribution.
4. For a 1-billion-token SNS at 10%/year initial rate: normal per-round reward ≈ 274 tokens; inflated reward ≈ 5,390,000 tokens — a ~19,675× over-mint of governance tokens distributed to neuron holders.
5. After this single event, `end_timestamp_seconds` is correctly set and subsequent rounds proceed normally. The damage is permanent and irreversible (tokens already distributed as maturity).

This is a **ledger conservation bug**: governance token supply is inflated far beyond the intended emission schedule, diluting all existing holders and breaking the economic model of the SNS.

### Likelihood Explanation

The `end_timestamp_seconds` field replaced the deprecated `round` field. Any SNS canister deployed before this field was introduced and subsequently upgraded to a version that reads it will have `end_timestamp_seconds: None` in its stored `latest_reward_event`. The periodic task (`run_periodic_tasks`) is called automatically by the IC timer/heartbeat — no external attacker action is required. The bug fires automatically on the first heartbeat after upgrade. The entry path is the IC's own timer mechanism, which is unprivileged and always active.

### Recommendation

Replace `unwrap_or_default()` with a fallback that reconstructs the correct timestamp from the deprecated `round` field and `genesis_timestamp_seconds` when `end_timestamp_seconds` is absent:

```rust
let reward_start_timestamp_seconds = self
    .latest_reward_event()
    .end_timestamp_seconds
    .unwrap_or_else(|| {
        let round = self.latest_reward_event().round;
        round
            .saturating_mul(round_duration_seconds)
            .saturating_add(self.proto.genesis_timestamp_seconds)
    });
```

Alternatively, add a one-time migration in `post_upgrade` that populates `end_timestamp_seconds` from `round` for any stored `RewardEvent` that lacks it.

### Proof of Concept

**State**: SNS canister with stored state:
- `genesis_timestamp_seconds = 1_700_000_000`
- `latest_reward_event = RewardEvent { round: 100, end_timestamp_seconds: None, ... }`
- `round_duration_seconds = 86_400` (1 day)
- `now = 1_700_000_000 + 86_400` (one day after genesis)

**Execution of `distribute_rewards`**:
1. `reward_start_timestamp_seconds = None.unwrap_or_default() = 0`
2. `new_rounds_count = (1_700_086_400 - 0) / 86_400 = 19_676`
3. Loop `i = 1..=19_676`:
   - `seconds_since_genesis = 86_400 * i + 0 - 1_700_000_000`
   - For `i ≤ 19_675`: result is negative → saturates to `0`
   - `reward_rate_at(Instant::from_seconds_since_genesis(0))` = maximum genesis rate
4. `rewards_purse_e8s = 19_676 × max_rate × 86_400 × supply`
5. For supply = 10^17 e8s: purse ≈ 5.39 × 10^14 e8s (5.39 million tokens) instead of ~274 tokens
6. All neurons receive ~19,676× their expected maturity increment
7. `latest_reward_event.end_timestamp_seconds` is set to `19_676 × 86_400 = 1_699_987_200` ≈ `now`
8. Subsequent calls to `should_distribute_rewards` correctly return `false` until the next round

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

**File:** rs/sns/governance/src/governance.rs (L5735-5739)
```rust
        let seconds_since_last_reward_event = now.saturating_sub(
            self.latest_reward_event()
                .end_timestamp_seconds
                .unwrap_or_default(),
        );
```

**File:** rs/sns/governance/src/governance.rs (L5808-5811)
```rust
        let reward_start_timestamp_seconds = self
            .latest_reward_event()
            .end_timestamp_seconds
            .unwrap_or_default();
```

**File:** rs/sns/governance/src/governance.rs (L5812-5814)
```rust
        let new_rounds_count = now
            .saturating_sub(reward_start_timestamp_seconds)
            .saturating_div(round_duration_seconds);
```

**File:** rs/sns/governance/src/governance.rs (L5862-5865)
```rust
                let seconds_since_genesis = round_duration_seconds
                    .saturating_mul(i)
                    .saturating_add(reward_start_timestamp_seconds)
                    .saturating_sub(self.proto.genesis_timestamp_seconds);
```

**File:** rs/sns/governance/src/governance.rs (L5867-5869)
```rust
                let current_reward_rate = voting_rewards_parameters.reward_rate_at(
                    crate::reward::Instant::from_seconds_since_genesis(i2d(seconds_since_genesis)),
                );
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1383-1383)
```text
  optional uint64 end_timestamp_seconds = 5;
```
