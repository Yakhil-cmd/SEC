Audit Report

## Title
Uninitialized `end_timestamp_seconds` in SNS `RewardEvent` Causes Massively Inflated First Reward Distribution - (File: rs/sns/governance/src/governance.rs)

## Summary
The SNS governance canister's `distribute_rewards` and `should_distribute_rewards` functions both call `.end_timestamp_seconds.unwrap_or_default()` on the latest reward event. When an SNS instance upgrades from an older binary that did not populate `end_timestamp_seconds`, the field deserializes as `None`, causing `unwrap_or_default()` to return `0`. This makes `new_rounds_count` equal to approximately `now / round_duration_seconds` (~19,675 for a 1-day round), producing a rewards purse of roughly 5.39× the total token supply, which is distributed as maturity to neurons and convertible to tokens via `disburse_maturity`.

## Finding Description
**Root cause — `should_distribute_rewards` (L5735–5738):**
```rust
let seconds_since_last_reward_event = now.saturating_sub(
    self.latest_reward_event()
        .end_timestamp_seconds
        .unwrap_or_default(),  // returns 0 when None
);
```
With `end_timestamp_seconds = None`, `seconds_since_last_reward_event ≈ 1.7×10⁹ s`, always exceeding any `round_duration_seconds`, so the function returns `true` unconditionally.

**Root cause — `distribute_rewards` (L5808–5814):**
```rust
let reward_start_timestamp_seconds = self
    .latest_reward_event()
    .end_timestamp_seconds
    .unwrap_or_default();  // returns 0 when None
let new_rounds_count = now
    .saturating_sub(reward_start_timestamp_seconds)
    .saturating_div(round_duration_seconds);
```
With `reward_start_timestamp_seconds = 0`, `new_rounds_count ≈ 19,675` (1-day round), and the loop at L5861–5872 accumulates rewards for all ~19,675 rounds, yielding a purse of ~5.39× supply.

**Why the guard is insufficient (L726–742):**
```rust
if proto.latest_reward_event.is_none() {
    proto.latest_reward_event = Some(RewardEvent {
        ...
        end_timestamp_seconds: Some(now),
        ...
    })
}
```
This guard only fires when `latest_reward_event` is entirely absent. If the canister already holds a `RewardEvent` from an older binary (i.e., `latest_reward_event` is `Some(...)` but `end_timestamp_seconds` is `None`), the guard is skipped and the stale `None` is carried forward.

**No migration in `canister_post_upgrade` (canister.rs L272–290):**
`canister_post_upgrade` calls `canister_init_` → `Governance::new()` with no additional field-level migration to backfill `end_timestamp_seconds`. The stale `None` persists into the live state.

**Reward event committed (L6084–6092):**
After the inflated distribution, `latest_reward_event` is written with `end_timestamp_seconds: Some(...)`, making the state permanent. Subsequent distributions proceed normally, but the maturity already credited to neurons is irreversible.

## Impact Explanation
This constitutes **illegal minting** of SNS tokens at a scale of ~5.39× the total token supply in a single automatic heartbeat tick. Neurons receive maturity proportional to their voting power; calling `disburse_maturity` converts this to tokens, permanently inflating supply and distorting governance voting power. For an SNS with a supply of billions of tokens, the minted value can far exceed $1M. This matches the Critical impact category: *Theft, permanent loss, illegal minting, or protocol insolvency involving exorbitant in-scope chain-key/ledger assets, especially over $1M.*

## Likelihood Explanation
The trigger is fully automatic — `distribute_rewards` is called from `run_periodic_tasks` on every heartbeat/timer tick. No user action, ingress message, or privileged access is required. The affected population is any SNS instance whose `latest_reward_event` was serialized by a binary predating the addition of `end_timestamp_seconds` to the `RewardEvent` proto. Since the field is `optional`, older serialized state deserializes with `end_timestamp_seconds = None`, and the first heartbeat after upgrade fires the vulnerable path.

## Recommendation
1. **Migrate the field on upgrade.** In `Governance::new()`, after the `is_none()` guard, add:
   ```rust
   if let Some(ref mut event) = proto.latest_reward_event {
       if event.end_timestamp_seconds.is_none() {
           event.end_timestamp_seconds = Some(now);
       }
   }
   ```
2. **Replace `unwrap_or_default()` with a safe fallback.** In both `should_distribute_rewards` (L5738) and `distribute_rewards` (L5811), replace `unwrap_or_default()` with `unwrap_or(now)` or `unwrap_or(self.proto.genesis_timestamp_seconds)` so a missing timestamp never implies Unix epoch 0.
3. **Add a `ValidGovernanceProto` validation rule** that rejects a `latest_reward_event` whose `end_timestamp_seconds` is `None`.

## Proof of Concept
1. Deploy an SNS governance canister with an older binary that does not set `end_timestamp_seconds` in `RewardEvent`.
2. Run one reward distribution cycle so that `latest_reward_event` is `Some(RewardEvent { end_timestamp_seconds: None, ... })`.
3. Upgrade the canister to the current binary. `canister_post_upgrade` → `canister_init_` → `Governance::new()` skips the initialization guard at L726 because `latest_reward_event.is_some()`.
4. On the next heartbeat, `should_distribute_rewards` computes `seconds_since_last_reward_event = now - 0 ≈ 1.7×10⁹ s > round_duration_seconds` → returns `true`.
5. `distribute_rewards` sets `reward_start_timestamp_seconds = 0`, computes `new_rounds_count ≈ 19,675` (1-day round), loops 19,675 times, and produces `rewards_purse_e8s ≈ 5.39 × supply_e8s`.
6. All neurons receive proportional maturity; calling `disburse_maturity` on any neuron mints tokens at the inflated rate.
7. The new `latest_reward_event` is written with `end_timestamp_seconds: Some(...)` at L6089, permanently committing the inflated state.

A deterministic integration test can reproduce this by: (a) constructing a `Governance` proto with `latest_reward_event: Some(RewardEvent { end_timestamp_seconds: None, ... })`, (b) calling `Governance::new()` with a current timestamp, (c) advancing the mock clock by one tick, and (d) asserting that `distribute_rewards` produces a `rewards_purse_e8s` far exceeding the token supply.