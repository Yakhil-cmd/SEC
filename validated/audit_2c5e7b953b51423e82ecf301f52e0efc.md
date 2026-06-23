### Title
Missing Post-Calculation Invariant Check Allows Distributed Rewards to Exceed Available Pool - (File: `rs/nns/governance/src/governance.rs`)

### Summary
The `calculate_voting_rewards` function in NNS governance computes individual neuron rewards using f64 floating-point arithmetic and accumulates them in `actually_distributed_e8s_equivalent`, but never verifies that this sum does not exceed `total_available_e8s_equivalent_float` before scheduling the rewards for distribution. This is the direct IC analog of the reported missing `zproceed ≤ I` bound check. A secondary instance exists in SNS governance, where the equivalent check is only a `debug_assert!` — absent in production builds.

### Finding Description

**Primary: NNS Governance (`rs/nns/governance/src/governance.rs`)**

In `calculate_voting_rewards`, the total available reward pool is computed as a raw f64 value:

```rust
let total_available_e8s_equivalent_float = (supply.get_e8s() as f64) * fraction
    + rolling_over_from_previous_reward_event_e8s_equivalent as f64;
```

Individual rewards are then computed per-neuron using f64 arithmetic:

```rust
let reward = (used_voting_rights * total_available_e8s_equivalent_float
    / total_voting_rights) as u64;
// ...
actually_distributed_e8s_equivalent += reward;
```

After the loop, `actually_distributed_e8s_equivalent` is recorded directly into the `RewardEvent` and passed to `schedule_pending_rewards_distribution` — with **no check** that it does not exceed `total_available_e8s_equivalent_float`:

```rust
let reward_event = RewardEvent {
    distributed_e8s_equivalent: actually_distributed_e8s_equivalent,
    total_available_e8s_equivalent: total_available_e8s_equivalent_float as u64,
    // ...
};
Some((reward_event, reward_distribution))
```

`total_voting_rights` is computed by `sum_weighted_voting_power` as a f64 sum over proposal ballots. Because f64 addition is non-associative, the sum of individual `used_voting_rights_i` values iterated in the reward loop (over a `HashMap`, with non-deterministic order) may not equal the `total_voting_rights` computed in a different traversal order. If `Σ used_voting_rights_i > total_voting_rights` due to f64 rounding, then `Σ reward_i` can exceed `total_available_e8s_equivalent_float`, violating the conservation invariant.

**Secondary: SNS Governance (`rs/sns/governance/src/governance.rs`)**

The SNS governance `distribute_rewards` function uses `Decimal` arithmetic (more precise), but the only guard is a `debug_assert!` — compiled out in production:

```rust
debug_assert!(
    i2d(distributed_e8s_equivalent) <= rewards_purse_e8s,
    "rewards distributed ({distributed_e8s_equivalent}) > purse ({rewards_purse_e8s})",
);
```

In a production release build, if `distributed_e8s_equivalent > rewards_purse_e8s` (e.g., due to a future arithmetic change or edge case), the violation is silently ignored and the over-distribution proceeds.

### Impact Explanation

If `actually_distributed_e8s_equivalent > total_available_e8s_equivalent_float as u64`, the governance canister schedules more neuron maturity than the reward pool permits. This maturity is later converted to ICP when neurons spawn or call `disburse_maturity`, causing unbounded ICP minting beyond the intended supply schedule — a **ledger conservation bug**. The excess per round is bounded by the number of voting neurons times the maximum per-neuron f64 rounding error (up to 1 e8 per neuron), which can accumulate to meaningful amounts across many neurons and reward rounds. For SNS governance, the same class of violation would inflate SNS token supply beyond the configured reward purse.

### Likelihood Explanation

Medium-low. The f64 non-associativity issue is real: `HashMap` iteration order is non-deterministic, so the order in which `used_voting_rights_i` values are summed in the reward loop differs from the order used in `sum_weighted_voting_power`. With a large number of neurons (NNS currently has hundreds of thousands), f64 rounding errors accumulate. The condition `Σ used_voting_rights_i > total_voting_rights` is achievable without any privileged access — any neuron holder voting on proposals participates in the computation. The SNS `debug_assert!` path requires only that a future code change introduces a Decimal rounding edge case, which is a latent risk.

### Recommendation

1. **NNS Governance**: After accumulating `actually_distributed_e8s_equivalent`, add a production-level bound check and cap:
   ```rust
   let cap = total_available_e8s_equivalent_float as u64;
   if actually_distributed_e8s_equivalent > cap {
       // log warning; cap to available
       actually_distributed_e8s_equivalent = cap;
   }
   ```
2. **NNS Governance**: Migrate reward arithmetic from f64 to `rust_decimal::Decimal` (as SNS governance already does) to eliminate floating-point non-associativity.
3. **SNS Governance**: Promote the `debug_assert!` at line 6004–6007 to a hard `assert!` or add an explicit production-level cap, so the invariant is enforced in release builds.

### Proof of Concept

1. Deploy NNS governance with a large number of neurons (e.g., 100,000+) each with distinct voting powers that are not powers of two (maximizing f64 rounding error).
2. Submit and vote on proposals with mixed topic weights (0.01×, 1×, 20×), causing `total_voting_rights` to be a non-representable f64 sum.
3. Trigger `distribute_voting_rewards_to_neurons` (called automatically each reward round).
4. Observe that `reward_event.distributed_e8s_equivalent > reward_event.total_available_e8s_equivalent` in the emitted `RewardEvent` — the missing invariant `actually_distributed_e8s_equivalent ≤ total_available_e8s_equivalent_float` was never enforced, and the excess maturity has been scheduled for distribution, to be minted as ICP upon neuron spawn/disburse. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** rs/nns/governance/src/governance.rs (L6653-6654)
```rust
        let total_available_e8s_equivalent_float = (supply.get_e8s() as f64) * fraction
            + rolling_over_from_previous_reward_event_e8s_equivalent as f64;
```

**File:** rs/nns/governance/src/governance.rs (L6724-6732)
```rust
                    let reward = (used_voting_rights * total_available_e8s_equivalent_float
                        / total_voting_rights) as u64;

                    reward_distribution.add_reward(neuron_id, reward);

                    // NOTE: This is the only reason we are checking the existence of neurons
                    // at this stage. Otherwise, we could defer until we distribute them in the
                    // schedule task.
                    actually_distributed_e8s_equivalent += reward;
```

**File:** rs/nns/governance/src/governance.rs (L6747-6758)
```rust
        let reward_event = RewardEvent {
            day_after_genesis,
            actual_timestamp_seconds: now,
            settled_proposals: considered_proposals,
            distributed_e8s_equivalent: actually_distributed_e8s_equivalent,
            total_available_e8s_equivalent: total_available_e8s_equivalent_float as u64,
            rounds_since_last_distribution: Some(rounds_since_last_distribution),
            latest_round_available_e8s_equivalent: Some(
                latest_round_available_e8s_equivalent_float as u64,
            ),
        };

```

**File:** rs/sns/governance/src/governance.rs (L6004-6007)
```rust
        debug_assert!(
            i2d(distributed_e8s_equivalent) <= rewards_purse_e8s,
            "rewards distributed ({distributed_e8s_equivalent}) > purse ({rewards_purse_e8s})",
        );
```
