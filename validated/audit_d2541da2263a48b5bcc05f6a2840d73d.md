### Title
Stale `total_supply` Snapshot Used for Voting-Reward Calculation During Async Await Window — (`rs/sns/governance/src/governance.rs` and `rs/nns/governance/src/timer_tasks/calculate_distributable_rewards.rs`)

### Summary

Both the SNS Governance canister and the NNS Governance canister fetch `total_supply` from the ledger via an inter-canister call, then use the returned value to calculate and distribute voting rewards. Because the IC's async model yields execution at every `await` point, other update calls (including `disburse_maturity`, `manage_neuron`, or any SNS token transfer) can execute and change the actual circulating supply between the moment `total_supply()` is awaited and the moment `distribute_rewards(supply)` is called with the stale snapshot. The reward pool is computed as `supply * reward_rate * duration`, so an inflated or deflated stale supply directly inflates or deflates the reward purse distributed to all voting neurons.

### Finding Description

**SNS Governance** (`rs/sns/governance/src/governance.rs`):

```rust
let should_distribute_rewards = self.should_distribute_rewards();  // line 5503
// ...
if should_distribute_rewards {
    match self.ledger.total_supply().await {          // line 5510 — YIELD POINT
        Ok(supply) => {
            self.distribute_rewards(supply);          // line 5513 — uses stale supply
        }
    }
}
```

Between lines 5510 and 5513, the canister is suspended. Any concurrent update message that the IC scheduler delivers during this window — e.g., a `disburse_maturity` call that mints new SNS tokens to a neuron owner, or a `MintSnsTokens` governance proposal execution — will mutate the ledger's actual supply. The `supply` value returned from the `total_supply()` call is therefore a snapshot from a moment that no longer reflects the state when `distribute_rewards` executes.

**NNS Governance** (`rs/nns/governance/src/timer_tasks/calculate_distributable_rewards.rs`):

```rust
let total_supply = self
    .governance
    .with_borrow(|governance| governance.get_ledger())
    .total_supply()
    .await;                                          // line 57 — YIELD POINT
match total_supply {
    Ok(total_supply) => {
        self.governance.with_borrow_mut(|governance| {
            governance.distribute_voting_rewards_to_neurons(total_supply);  // line 61
        });
    }
}
```

The same pattern applies: the `total_supply` snapshot is taken at the ledger's state at the time of the inter-canister call, but `distribute_voting_rewards_to_neurons` is called with that snapshot after an arbitrary number of other messages may have executed.

The reward pool calculation in both canisters multiplies the stale supply directly:

```rust
// SNS (rs/sns/governance/src/governance.rs ~line 5871)
result += current_reward_rate * voting_rewards_parameters.round_duration() * supply;

// NNS (rs/nns/governance/src/governance.rs ~line 6653)
let total_available_e8s_equivalent_float = (supply.get_e8s() as f64) * fraction + ...;
```

### Impact Explanation

An attacker who can influence the ledger's `total_supply` at the right moment — for example, by triggering a large `disburse_maturity` finalization (which mints new tokens) or a `MintSnsTokens` proposal execution immediately before the governance timer fires — can cause the reward pool to be calculated against a supply figure that is either larger or smaller than the true circulating supply at the time of distribution. If the supply is inflated (e.g., a large mint completes just before the `total_supply` call), the reward pool is over-sized and all voting neurons receive more maturity than they should. If the supply is deflated (e.g., a large burn completes just before the call), neurons receive less. Over many reward rounds, this compounds into a material ledger conservation violation: the total maturity credited to neurons diverges from the intended fraction of the true circulating supply.

### Likelihood Explanation

The SNS `run_periodic_tasks` is called on every heartbeat. The NNS `CalculateDistributableRewardsTask` fires on a recurring timer. Both are triggered automatically without any user action. Any concurrent update call that changes the ledger supply — including ordinary user-initiated `disburse_maturity` finalizations, which are also triggered by the same heartbeat — can land in the yield window. The IC scheduler is free to interleave messages from any caller, including unprivileged users. No special privilege is required: any SNS token holder can call `disburse_maturity`, and the finalization timer fires automatically. The window is narrow (one inter-canister round-trip), but the event fires every heartbeat/timer tick, making repeated exposure certain over the lifetime of the canister.

### Recommendation

1. **Re-check the distribution condition after the await.** After `total_supply().await` returns, re-evaluate `should_distribute_rewards()` before proceeding. If the condition is no longer true (e.g., another concurrent invocation already distributed rewards), abort.

2. **Use a guard/lock before the await.** Set a boolean flag (e.g., `reward_distribution_in_progress`) synchronously before the `await`, and clear it after. This prevents a second concurrent invocation from also fetching supply and distributing rewards simultaneously.

3. **Accept the inherent staleness but bound its impact.** If exact supply precision is not required, document the known staleness and add an assertion that the fetched supply is within a reasonable tolerance of the governance canister's own tracked metrics (e.g., `cached_metrics.total_supply_icp`).

### Proof of Concept

1. An SNS is deployed with a large neuron holding maturity ready for disbursement (finalization delay elapsed).
2. The IC scheduler fires the governance heartbeat. `run_periodic_tasks` begins executing.
3. `should_distribute_rewards()` returns `true` (line 5503).
4. `self.ledger.total_supply().await` is issued (line 5510). The governance canister suspends.
5. While suspended, the IC scheduler delivers the `maybe_finalize_disburse_maturity` timer callback (also triggered by the same heartbeat cycle). This mints a large number of SNS tokens to the neuron owner via the ledger, increasing `total_supply` by, say, 10%.
6. The ledger responds to the governance canister's `total_supply` query with the pre-mint value (the query was issued before step 5 completed).
7. `distribute_rewards(supply)` is called with the stale (pre-mint) supply (line 5513).
8. The reward pool is computed as `stale_supply * rate * duration`, which is ~10% smaller than it should be. All voting neurons receive proportionally fewer rewards than the protocol intends.
9. Conversely, if the attacker times a large burn (e.g., via `retrieve_btc` on a ckToken SNS) to complete between steps 4 and 6, the stale supply is larger than the true supply, and neurons receive more rewards than intended. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

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

**File:** rs/sns/governance/src/governance.rs (L5854-5876)
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
        debug_assert!(rewards_purse_e8s >= dec!(0), "{}", rewards_purse_e8s);
```

**File:** rs/nns/governance/src/timer_tasks/calculate_distributable_rewards.rs (L52-71)
```rust
    async fn execute(self) -> (Duration, Self) {
        let total_supply = self
            .governance
            .with_borrow(|governance| governance.get_ledger())
            .total_supply()
            .await;
        match total_supply {
            Ok(total_supply) => {
                self.governance.with_borrow_mut(|governance| {
                    governance.distribute_voting_rewards_to_neurons(total_supply);
                });
            }
            Err(err) => {
                println!(
                    "{}Error when getting total ICP supply: {}",
                    LOG_PREFIX,
                    GovernanceError::from(err)
                )
            }
        }
```

**File:** rs/nns/governance/src/governance.rs (L6647-6654)
```rust
        let fraction: f64 = days
            .map(crate::reward::calculation::rewards_pool_to_distribute_in_supply_fraction_for_one_day)
            .sum();

        let rolling_over_from_previous_reward_event_e8s_equivalent =
            latest_reward_event.e8s_equivalent_to_be_rolled_over();
        let total_available_e8s_equivalent_float = (supply.get_e8s() as f64) * fraction
            + rolling_over_from_previous_reward_event_e8s_equivalent as f64;
```
