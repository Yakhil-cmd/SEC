### Title
Stale `deciding_voting_power` After Automatic Reward Distribution to Neurons with `auto_stake_maturity` - (`File: rs/nns/governance/src/reward/distribution.rs`)

---

### Summary

When the NNS governance canister distributes voting rewards to neurons that have `auto_stake_maturity` enabled, the reward is silently added to `staked_maturity_e8s_equivalent`, which directly increases `stake_e8s()` and therefore `potential_voting_power`. However, `voting_power_refreshed_timestamp_seconds` is **never updated** during this automatic distribution. As a result, the `deciding_voting_power` — which is `potential_voting_power * adjustment_factor(time_since_last_refresh)` — becomes stale: the potential grows but the deciding power remains penalized by the old refresh timestamp, causing incorrect reward distribution in subsequent rounds.

---

### Finding Description

The NNS governance reward distribution pipeline in `continue_processing` (in `rs/nns/governance/src/reward/distribution.rs`) adds maturity rewards to neurons:

```rust
if auto_stake {
    neuron.staked_maturity_e8s_equivalent = Some(
        neuron.staked_maturity_e8s_equivalent.unwrap_or_default()
            .saturating_add(reward_e8s),
    );
} else {
    neuron.maturity_e8s_equivalent =
        neuron.maturity_e8s_equivalent.saturating_add(reward_e8s);
}
``` [1](#0-0) 

For neurons with `auto_stake_maturity = true`, `staked_maturity_e8s_equivalent` is increased. This field is included in `stake_e8s()`:

```rust
pub fn stake_e8s(&self) -> u64 {
    neuron_stake_e8s(
        self.cached_neuron_stake_e8s,
        self.neuron_fees_e8s,
        self.staked_maturity_e8s_equivalent,
    )
}
``` [2](#0-1) 

And `stake_e8s()` is the direct input to `potential_and_deciding_voting_power`:

```rust
let stake_e8s = self.stake_e8s();
let boost = dissolve_delay_bonus_multiplier(...) * age_bonus_multiplier(...);
let mut potential_voting_power = Decimal::from(stake_e8s) * boost;
...
let adjustment_factor = voting_power_economics
    .deciding_voting_power_adjustment_factor(time_since_last_refreshed);
let deciding_voting_power = adjustment_factor * potential_voting_power.floor();
``` [3](#0-2) 

The `adjustment_factor` is computed from `voting_power_refreshed_timestamp_seconds`:

```rust
let time_since_last_refreshed = Duration::from_secs(
    now_seconds.saturating_sub(self.voting_power_refreshed_timestamp_seconds),
);
``` [4](#0-3) 

The `deciding_voting_power` is what is used to assign ballot weights when a new proposal is created, and it is also used to compute each neuron's share of voting rewards in subsequent rounds. When `staked_maturity_e8s_equivalent` grows via automatic reward distribution but `voting_power_refreshed_timestamp_seconds` is not updated, the neuron's `potential_voting_power` increases while its `deciding_voting_power` remains penalized by the stale refresh timestamp. This is the exact analog of the reported Solidity bug: a key metric (`deciding_voting_power`) becomes stale when an underlying input (`staked_maturity_e8s_equivalent`) changes through an external path (reward distribution) that does not trigger a recalculation/refresh.

The `refresh_voting_power` function, which is the only way to update `voting_power_refreshed_timestamp_seconds`, requires the neuron's controller or a hotkey to call it explicitly:

```rust
fn refresh_voting_power(&mut self, neuron_id: &NeuronId, caller: &PrincipalId)
    -> Result<(), GovernanceError> {
    let is_authorized =
        self.with_neuron(neuron_id, |neuron| neuron.is_authorized_to_vote(caller))?;
    ...
    neuron.refresh_voting_power(now_seconds);
}
``` [5](#0-4) 

The `refresh_voting_power` method on the neuron itself only sets the timestamp:

```rust
pub(crate) fn refresh_voting_power(&mut self, now_seconds: u64) {
    self.voting_power_refreshed_timestamp_seconds = now_seconds;
}
``` [6](#0-5) 

---

### Impact Explanation

**Incorrect reward distribution:** Voting rewards in subsequent rounds are proportional to `deciding_voting_power` (which is `potential_voting_power * adjustment_factor`). A neuron with `auto_stake_maturity` that has not explicitly refreshed its voting power will have a growing `potential_voting_power` (due to accumulated staked maturity) but a shrinking `adjustment_factor` (due to the stale refresh timestamp). This means the neuron receives fewer rewards than it should relative to its actual stake, and other neurons receive proportionally more. The reward distribution is incorrect.

**Incorrect proposal ballot weights:** When a new proposal is created, each neuron's ballot is assigned its current `deciding_voting_power`. A neuron with `auto_stake_maturity` that has not refreshed will have a lower `deciding_voting_power` than its actual stake warrants, reducing its governance influence.

**Governance manipulation surface:** An adversary who understands this mechanic can exploit it by ensuring their own neurons always refresh (maintaining full `deciding_voting_power`) while other neurons with `auto_stake_maturity` accumulate stale timestamps, gaining disproportionate governance influence and rewards.

---

### Likelihood Explanation

This is a **medium-to-high likelihood** issue. The `auto_stake_maturity` feature is a standard, documented NNS neuron configuration. Reward distribution happens automatically every day via `run_periodic_tasks` → `distribute_voting_rewards_to_neurons`. Any neuron with `auto_stake_maturity = true` that does not actively call `RefreshVotingPower`, vote directly, or update following will accumulate this staleness over time. The staleness grows continuously and silently — there is no on-chain alert or correction mechanism. The effect compounds: each reward round increases `staked_maturity_e8s_equivalent` (and thus `potential_voting_power`) without updating the refresh timestamp, widening the gap between potential and deciding voting power. [7](#0-6) 

---

### Recommendation

When `auto_stake_maturity` is true and a reward is added to `staked_maturity_e8s_equivalent` during `continue_processing`, the `voting_power_refreshed_timestamp_seconds` should also be updated to the current time. This mirrors the fix in the external report: the stale metric should be recalculated whenever the underlying inputs change through an automatic path. Alternatively, the reward distribution timestamp could be stored and used as a proxy for the refresh timestamp in the deciding voting power calculation for neurons with `auto_stake_maturity`.

---

### Proof of Concept

1. Neuron A has `auto_stake_maturity = true`, `cached_neuron_stake_e8s = 1_000_000_000`, `staked_maturity_e8s_equivalent = 0`, `voting_power_refreshed_timestamp_seconds = T0`.
2. At time `T0 + 6 months + 1 day`, the daily reward distribution runs. Neuron A receives `reward_e8s = 10_000_000` added to `staked_maturity_e8s_equivalent`. `voting_power_refreshed_timestamp_seconds` remains `T0`.
3. A new proposal is created at `T0 + 6 months + 1 day`. Neuron A's `potential_voting_power` is now computed from `stake_e8s() = 1_010_000_000`, but `adjustment_factor` is computed from `time_since_last_refreshed = 6 months + 1 day`, which is in the penalty zone per `VotingPowerEconomics::DEFAULT` (`start_reducing_voting_power_after_seconds = 6 months`).
4. Neuron A's `deciding_voting_power` is therefore `< potential_voting_power`, despite having just received a reward that increased its stake. Its ballot weight is understated.
5. In the next reward round, Neuron A's reward share is computed from its understated `deciding_voting_power`, so it receives fewer rewards than its actual stake warrants. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/nns/governance/src/reward/distribution.rs (L154-188)
```rust
    fn continue_processing(
        &mut self,
        neuron_store: &mut NeuronStore,
        is_over_instructions_limit: fn() -> bool,
    ) {
        while let Some((id, reward_e8s)) = self.rewards.pop_first() {
            match neuron_store.with_neuron_mut(&id, |neuron| {
                let auto_stake = neuron.auto_stake_maturity.unwrap_or(false);
                if auto_stake {
                    neuron.staked_maturity_e8s_equivalent = Some(
                        neuron
                            .staked_maturity_e8s_equivalent
                            .unwrap_or_default()
                            .saturating_add(reward_e8s),
                    );
                } else {
                    neuron.maturity_e8s_equivalent =
                        neuron.maturity_e8s_equivalent.saturating_add(reward_e8s);
                }
            }) {
                Ok(_) => {}
                Err(e) => {
                    println!(
                        "{}Error rewarding neuron {:?} during reward_distribution.\
                    This should not be possible as neuron existence is checked when \
                    rewards are calculated: {}",
                        LOG_PREFIX, id, e
                    );
                }
            };
            if is_over_instructions_limit() {
                break;
            }
        }
    }
```

**File:** rs/nns/governance/src/neuron/types.rs (L376-399)
```rust
        let stake_e8s = self.stake_e8s();
        let boost = dissolve_delay_bonus_multiplier(self.dissolve_delay_seconds(now_seconds))
            * age_bonus_multiplier(self.age_seconds(now_seconds));
        let mut potential_voting_power = Decimal::from(stake_e8s) * boost;

        // 8 Year Gang bonus. Cap the bonus base to the current stake because
        // rejection fees can cause the bonus base to exceed stake_e8s.
        if is_mission_70_voting_rewards_enabled() {
            let eight_year_gang_bonus_base_e8s = self.eight_year_gang_bonus_base_e8s.min(stake_e8s);
            potential_voting_power +=
                Decimal::from(eight_year_gang_bonus_base_e8s) / Decimal::from(10) * boost;
        }

        // For DECIDING voting power.
        let adjustment_factor: Decimal = {
            let time_since_last_refreshed = Duration::from_secs(
                now_seconds.saturating_sub(self.voting_power_refreshed_timestamp_seconds),
            );

            voting_power_economics
                .deciding_voting_power_adjustment_factor(time_since_last_refreshed)
        };

        let deciding_voting_power = adjustment_factor * potential_voting_power.floor();
```

**File:** rs/nns/governance/src/neuron/types.rs (L515-517)
```rust
    pub(crate) fn refresh_voting_power(&mut self, now_seconds: u64) {
        self.voting_power_refreshed_timestamp_seconds = now_seconds;
    }
```

**File:** rs/nns/governance/src/neuron/types.rs (L973-979)
```rust
    pub fn stake_e8s(&self) -> u64 {
        neuron_stake_e8s(
            self.cached_neuron_stake_e8s,
            self.neuron_fees_e8s,
            self.staked_maturity_e8s_equivalent,
        )
    }
```

**File:** rs/nns/governance/src/governance.rs (L5675-5710)
```rust
    fn refresh_voting_power(
        &mut self,
        neuron_id: &NeuronId,
        caller: &PrincipalId,
    ) -> Result<(), GovernanceError> {
        let is_authorized =
            self.with_neuron(neuron_id, |neuron| neuron.is_authorized_to_vote(caller))?;
        if !is_authorized {
            return Err(GovernanceError::new_with_message(
                ErrorType::NotAuthorized,
                format!(
                    "The caller ({}) is not authorized to refresh the voting power of neuron {}.",
                    caller, neuron_id.id,
                ),
            ));
        }

        let now_seconds = self.env.now();

        let result = self.with_neuron_mut(neuron_id, |neuron| {
            neuron.refresh_voting_power(now_seconds);
        });

        if let Err(err) = result {
            return Err(GovernanceError::new_with_message(
                ErrorType::NotFound,
                format!(
                    "Tried to refresh the voting power of neuron {}, \
                     but was unable to find it: {:?}",
                    neuron_id.id, err,
                ),
            ));
        }

        Ok(())
    }
```

**File:** rs/nns/governance/src/network_economics.rs (L296-298)
```rust
    pub const DEFAULT_START_REDUCING_VOTING_POWER_AFTER_SECONDS: u64 = 6 * ONE_MONTH_SECONDS;

    pub const DEFAULT_CLEAR_FOLLOWING_AFTER_SECONDS: u64 = ONE_MONTH_SECONDS;
```
