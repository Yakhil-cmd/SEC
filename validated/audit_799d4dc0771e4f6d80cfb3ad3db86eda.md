Audit Report

## Title
Silent Arithmetic Underflow in SNS Reward Round Epoch Calculation Inflates Voting Rewards - (File: rs/sns/governance/src/governance.rs)

## Summary
In `distribute_rewards`, when `latest_reward_event.end_timestamp_seconds` is `None` (possible for SNS instances whose stored state predates the field), `unwrap_or_default()` yields 0. The subsequent `saturating_sub(genesis_timestamp_seconds)` silently clamps to 0 for all rounds where `round_duration_seconds * i < genesis_timestamp_seconds`, causing `reward_rate_at` to be called with `seconds_since_genesis = 0` and return the maximum initial reward rate. Combined with an unbounded `new_rounds_count` (also derived from `reward_start_timestamp_seconds = 0`), this inflates the SNS rewards purse across thousands of back-filled rounds.

## Finding Description

**Root cause — `unwrap_or_default` on an optional field:**

At `rs/sns/governance/src/governance.rs` L5808–5811, `reward_start_timestamp_seconds` is set to 0 when `end_timestamp_seconds` is `None`:

```rust
let reward_start_timestamp_seconds = self
    .latest_reward_event()
    .end_timestamp_seconds
    .unwrap_or_default();   // → 0 when field is None
```

**Unbounded `new_rounds_count`:**

At L5812–5814, `new_rounds_count` is computed as `now / round_duration_seconds` when `reward_start_timestamp_seconds = 0`. For `now ≈ 1_700_000_000` and `round_duration_seconds = 604_800`, this yields ~2,812 rounds — all processed in a single call with no cap.

**Saturating underflow in the reward loop:**

At L5861–5865:

```rust
for i in 1..=new_rounds_count {
    let seconds_since_genesis = round_duration_seconds
        .saturating_mul(i)
        .saturating_add(reward_start_timestamp_seconds)  // + 0
        .saturating_sub(self.proto.genesis_timestamp_seconds); // underflows → 0
```

For all rounds where `round_duration_seconds * i < genesis_timestamp_seconds` (rounds 1 through ~2,810 in the example), the result saturates to 0.

**`reward_rate_at(0)` returns maximum initial rate:**

`GENESIS` is defined as `Instant::from_seconds_since_genesis(dec!(0))` (reward.rs L35). Inside `reward_rate_at` (reward.rs L202–241), `time_since_genesis = now - GENESIS = 0`. With `s = 1`, `s2 = 1`, the function returns `final_reward_rate + (initial_reward_rate - final_reward_rate) = initial_reward_rate` — the maximum rate — for all ~2,810 underflowed rounds instead of the correct time-adjusted rate.

**Why the constructor guard is insufficient:**

The `Governance::new` constructor at L726–742 only inserts a dummy `RewardEvent` with `end_timestamp_seconds: Some(now)` when `proto.latest_reward_event.is_none()`. For any SNS instance that already has a stored `latest_reward_event` with `end_timestamp_seconds: None` (legacy state from before the field was introduced), the guard is never reached and the field remains `None`.

**No upper bound on `new_rounds_count`:** No cap exists in the code path; the loop runs for all ~2,812 back-filled rounds.

## Impact Explanation

Every affected SNS governance canister computes `rewards_purse_e8s` using `initial_reward_rate` for ~2,810 rounds instead of the correct time-adjusted rate. This causes the canister to mint and distribute far more SNS tokens as voting rewards than the tokenomics schedule permits — constituting illegal minting of SNS governance tokens. Neurons that voted receive excess maturity disbursable as tokens, permanently diluting the token supply beyond the intended schedule. This matches the allowed impact: **illegal minting / significant SNS security impact with concrete protocol harm** — High ($2,000–$10,000), escalating toward Critical if the affected SNS token supply is large enough to push excess minting above $1M.

## Likelihood Explanation

No attacker action is required. `distribute_rewards` is triggered automatically by the periodic timer. Any SNS instance whose persisted `latest_reward_event.end_timestamp_seconds` is `None` — due to a state upgrade that did not migrate the field — is unconditionally affected on the next timer tick. The precondition (legacy SNS state with `None` field) is realistic for SNS instances deployed before `end_timestamp_seconds` was added to the `RewardEvent` proto. The exploit is fully automatic and repeatable every reward round until the state is corrected.

## Recommendation

1. Replace the silent `unwrap_or_default()` with an explicit guard that treats `None` as a fatal misconfiguration or falls back to `genesis_timestamp_seconds`:

```rust
let reward_start_timestamp_seconds = match self
    .latest_reward_event()
    .end_timestamp_seconds
{
    Some(ts) => ts,
    None => {
        log!(ERROR, "end_timestamp_seconds is None; cannot distribute rewards safely.");
        return;
    }
};
```

2. Add an explicit underflow check before the subtraction:

```rust
let round_end = round_duration_seconds
    .saturating_mul(i)
    .saturating_add(reward_start_timestamp_seconds);
if round_end < self.proto.genesis_timestamp_seconds {
    log!(ERROR, "round end {} precedes genesis {}; aborting", round_end, self.proto.genesis_timestamp_seconds);
    return;
}
let seconds_since_genesis = round_end - self.proto.genesis_timestamp_seconds;
```

3. Add a state migration in the SNS upgrade path to populate `end_timestamp_seconds` for any stored `RewardEvent` where it is `None`, using `actual_timestamp_seconds` as a safe fallback.

## Proof of Concept

1. Deploy an SNS with `genesis_timestamp_seconds = 1_700_000_000`, `round_duration_seconds = 604_800`.
2. Simulate legacy state: set `proto.latest_reward_event = Some(RewardEvent { end_timestamp_seconds: None, round: 5, actual_timestamp_seconds: 1_703_000_000, ... })` (as would exist for an SNS upgraded without field migration).
3. Advance `env.now()` to `1_703_604_800` (one round past the last event's actual timestamp).
4. Call `distribute_rewards`.
5. Observe: `reward_start_timestamp_seconds = 0`, `new_rounds_count = 1_703_604_800 / 604_800 ≈ 2,817`.
6. For rounds 1–2,810: `seconds_since_genesis = 0`, `reward_rate_at` returns `initial_reward_rate`.
7. `rewards_purse_e8s` is inflated by `(initial_reward_rate - correct_rate) * round_duration * supply * 2810`.
8. Voting neurons receive excess maturity; disbursement mints tokens beyond the intended supply schedule.

This can be reproduced as a deterministic unit test in `rs/sns/governance/src/governance.rs` by constructing a `Governance` instance with the legacy state described above and asserting that `rewards_purse_e8s` exceeds the correct value.