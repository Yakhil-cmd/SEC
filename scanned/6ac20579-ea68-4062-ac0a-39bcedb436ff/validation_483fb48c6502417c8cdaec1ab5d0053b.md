### Title
Uninitialized `end_timestamp_seconds` in SNS `RewardEvent` Causes Massively Inflated First Reward Distribution - (File: rs/sns/governance/src/governance.rs)

---

### Summary

The SNS governance canister's `distribute_rewards` function computes the number of elapsed reward rounds using `latest_reward_event.end_timestamp_seconds.unwrap_or_default()`. When this optional field is `None` — which occurs for any SNS instance whose persisted `RewardEvent` predates the introduction of `end_timestamp_seconds` — the default value of `0` is used as the reward-period start. This causes `new_rounds_count` to equal `now / round_duration_seconds` (approximately 19,675 rounds for a 1-day period at current Unix time), and the resulting rewards purse is computed as roughly **5.39× the total token supply**. This maturity is distributed to neurons and is convertible to tokens via `disburse_maturity`, constituting a chain-fusion mint/burn accounting bug.

---

### Finding Description

**Root cause — `distribute_rewards` in `rs/sns/governance/src/governance.rs`:**

```rust
let reward_start_timestamp_seconds = self
    .latest_reward_event()
    .end_timestamp_seconds
    .unwrap_or_default();          // ← returns 0 when field is None
let new_rounds_count = now
    .saturating_sub(reward_start_timestamp_seconds)
    .saturating_div(round_duration_seconds);
``` [1](#0-0) 

The same `unwrap_or_default()` pattern appears in `should_distribute_rewards`, which gates the call:

```rust
let seconds_since_last_reward_event = now.saturating_sub(
    self.latest_reward_event()
        .end_timestamp_seconds
        .unwrap_or_default(),
);
``` [2](#0-1) 

When `end_timestamp_seconds` is `None`, `seconds_since_last_reward_event` equals the full Unix timestamp of `now` (~1.7 × 10⁹ s), which is always greater than any `round_duration_seconds`, so `should_distribute_rewards` returns `true` and `distribute_rewards` proceeds.

**Why `end_timestamp_seconds` can be `None` after upgrade:**

`Governance::new()` only initializes `latest_reward_event` when the field is entirely absent (`is_none()`):

```rust
if proto.latest_reward_event.is_none() {
    proto.latest_reward_event = Some(RewardEvent {
        ...
        end_timestamp_seconds: Some(now),
        ...
    })
}
``` [3](#0-2) 

If the canister already holds a `RewardEvent` in stable memory (i.e., `latest_reward_event` is `Some(...)`) but that event was written by an older binary that did not populate `end_timestamp_seconds`, the guard is skipped and the `None` value is carried forward. `canister_post_upgrade` calls `canister_init_` which calls `Governance::new()` — no separate migration corrects the stale field: [4](#0-3) 

**Reward purse inflation:**

With `reward_start_timestamp_seconds = 0` and `now ≈ 1.7 × 10⁹ s`:

- For a 1-day round: `new_rounds_count ≈ 19,675`
- For a 7-day round: `new_rounds_count ≈ 2,811`

The inner loop accumulates:

```rust
for i in 1..=new_rounds_count {
    result += current_reward_rate * voting_rewards_parameters.round_duration() * supply;
}
``` [5](#0-4) 

At an initial reward rate of 10 %/year, the total purse converges to approximately **5.39 × supply** (the integral of the rate from Unix epoch 0 to now). For a supply of up to ~34 billion tokens this does not overflow `u64`, so the function completes successfully and writes the inflated `RewardEvent`: [6](#0-5) 

The inflated maturity is then distributed to all voting neurons proportionally. Neurons can convert maturity to tokens via `disburse_maturity`, effectively minting ~5.39× the intended supply.

---

### Impact Explanation

- **Token supply inflation:** Neurons receive maturity equivalent to ~5.39× the total SNS token supply in a single heartbeat tick after upgrade. Converting this maturity mints tokens far beyond the intended schedule.
- **Governance power distortion:** Neurons that hold large stakes receive disproportionate maturity, permanently skewing voting power after conversion.
- **Irreversibility:** Once maturity is credited to neurons and the `latest_reward_event` is updated with `end_timestamp_seconds: Some(...)`, the state is committed. Subsequent distributions proceed normally, but the damage from the first distribution is permanent.

---

### Likelihood Explanation

- **Trigger is automatic:** `distribute_rewards` is called from `run_periodic_tasks`, which is invoked by the canister's heartbeat/timer. No user action is required; the first heartbeat after upgrade fires the vulnerable path.
- **Affected population:** Any SNS instance whose `latest_reward_event` was written before `end_timestamp_seconds` was added to the `RewardEvent` proto message. The field is `optional`, so older serialized state deserializes with `end_timestamp_seconds = None`.
- **No privileged access needed:** The heartbeat is an internal IC mechanism; no ingress message or special role is required to trigger the bug.

---

### Recommendation

1. **Migrate the field on upgrade.** In `canister_post_upgrade` (or inside `Governance::new()` after the `is_none()` guard), add an explicit check:

   ```rust
   if let Some(ref mut event) = proto.latest_reward_event {
       if event.end_timestamp_seconds.is_none() {
           event.end_timestamp_seconds = Some(now);
       }
   }
   ```

2. **Replace `unwrap_or_default()` with a safe fallback.** In both `should_distribute_rewards` and `distribute_rewards`, replace `unwrap_or_default()` with `unwrap_or(now)` (or `unwrap_or(genesis_timestamp_seconds)`) so that a missing timestamp never implies Unix epoch 0.

3. **Add a `ValidGovernanceProto` validation rule** that rejects a `latest_reward_event` whose `end_timestamp_seconds` is `None`, forcing operators to supply a valid value before the canister starts.

---

### Proof of Concept

1. Deploy an SNS governance canister with an older binary that does not set `end_timestamp_seconds` in `RewardEvent`.
2. Run one reward distribution cycle so that `latest_reward_event` is `Some(RewardEvent { end_timestamp_seconds: None, ... })`.
3. Upgrade the canister to the current binary. `canister_post_upgrade` → `canister_init_` → `Governance::new()` skips the initialization guard because `latest_reward_event.is_some()`.
4. On the next heartbeat, `should_distribute_rewards` computes `seconds_since_last_reward_event = now - 0 ≈ 1.7 × 10⁹ s > round_duration_seconds` → returns `true`.
5. `distribute_rewards` sets `reward_start_timestamp_seconds = 0`, computes `new_rounds_count ≈ 19,675` (1-day round), loops 19,675 times, and produces `rewards_purse_e8s ≈ 5.39 × supply_e8s`.
6. All neurons receive proportional maturity; calling `disburse_maturity` on any neuron mints tokens at the inflated rate. [1](#0-0) [7](#0-6) [3](#0-2) [4](#0-3)

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

**File:** rs/sns/governance/src/governance.rs (L6084-6092)
```rust
        self.proto.latest_reward_event = Some(RewardEvent {
            round: new_reward_event_round,
            actual_timestamp_seconds: now,
            settled_proposals: considered_proposals,
            distributed_e8s_equivalent,
            end_timestamp_seconds: Some(reward_event_end_timestamp_seconds),
            rounds_since_last_distribution: Some(new_rounds_count),
            total_available_e8s_equivalent,
        })
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
