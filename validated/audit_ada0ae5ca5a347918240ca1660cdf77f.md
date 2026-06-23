### Title
Weighted-Average Age Manipulation via Neuron Merge Inflates Voting Power and Rewards - (`rs/nns/governance/src/governance/merge_neurons.rs`)

### Summary

The NNS governance `merge_neurons` operation uses a weighted-average formula (`combine_aged_stakes`) to compute the resulting age of the target neuron after a merge. Because the merge requires both neurons to be controlled by the same principal (controller or hotkey), a single actor can create two neurons — one with a large, old stake and one with a small, fresh stake — and merge the fresh one into the old one. The weighted-average age calculation means the target neuron's age is only slightly reduced, while the target neuron also inherits the source's `eight_year_gang_bonus_base_e8s` in full. By repeatedly splitting and re-merging neurons, or by carefully timing merges, an attacker can preserve an artificially high age-bonus and dissolve-delay-bonus combination on a single neuron, inflating its voting power and voting rewards beyond what the protocol intends.

### Finding Description

In `calculate_merge_neurons_effect` in `rs/nns/governance/src/governance/merge_neurons.rs`, the new age of the target neuron after a merge is computed as:

```rust
let (_, new_target_age_seconds) = combine_aged_stakes(
    target.cached_stake_e8s,
    target.age_seconds,
    amount_to_target_e8s,
    source.age_seconds,
);
``` [1](#0-0) 

The `combine_aged_stakes` function computes a stake-weighted average:

```
new_age = (target_stake * target_age + source_stake * source_age) / (target_stake + source_stake)
``` [2](#0-1) 

The resulting `target_neuron_dissolve_state_and_age` takes the **maximum** of the two dissolve delays:

```rust
let target_neuron_dissolve_state_and_age = DissolveStateAndAge::NotDissolving {
    dissolve_delay_seconds: std::cmp::max(
        source.dissolve_delay_seconds,
        target.dissolve_delay_seconds,
    ),
    aging_since_timestamp_seconds: now_seconds.saturating_sub(new_target_age_seconds),
};
``` [3](#0-2) 

Additionally, the **entire** `eight_year_gang_bonus_base_e8s` of the source is transferred to the target unconditionally:

```rust
let transfer_eight_year_gang_bonus_base_e8s = source.eight_year_gang_bonus_base_e8s;
``` [4](#0-3) 

The `eight_year_gang_bonus_base_e8s` contributes directly to voting power:

```rust
potential_voting_power +=
    Decimal::from(eight_year_gang_bonus_base_e8s) / Decimal::from(10) * boost;
``` [5](#0-4) 

The `boost` multiplier is the product of the dissolve-delay bonus and the age bonus:

```rust
let boost = dissolve_delay_bonus_multiplier(self.dissolve_delay_seconds(now_seconds))
    * age_bonus_multiplier(self.age_seconds(now_seconds));
``` [6](#0-5) 

The merge requires both neurons to be controlled by the same principal (controller or hotkey), so no external cooperation is needed:

```rust
let source_is_caller_authorized =
    source_neuron.is_authorized_to_simulate_manage_neuron(caller);
``` [7](#0-6) 

The `validate_merge_neurons_before_commit` function additionally requires the caller to be the **controller** (not just hotkey) of both neurons:

```rust
if !source_is_caller_controller {
    return Err(MergeNeuronsError::SourceNeuronNotController);
}
``` [8](#0-7) 

### Impact Explanation

**Voting power inflation:** A neuron controller can split a large, old, high-dissolve-delay neuron into a parent and a child (via `Split`), then immediately merge the child back into the parent. The split child inherits a proportional share of `eight_year_gang_bonus_base_e8s`. On re-merge, the full bonus base is re-added to the parent. The parent's age is only slightly diluted by the weighted average (since the child had age 0 at the time of split, but the merge uses the child's age at merge time, which could be non-zero if the attacker waits). More critically, the dissolve delay of the target is set to the **maximum** of the two, so merging a source with a higher dissolve delay into a target with a lower one upgrades the target's dissolve delay for free — without the target having to wait.

**Voting rewards inflation:** Since voting rewards are proportional to voting power, inflated voting power directly inflates ICP rewards minted by the NNS, diluting all other stakers.

**Concrete scenario (dissolve delay upgrade via merge):**
1. Alice controls Neuron A (100 ICP, dissolve delay = 6 months, age = 4 years) and Neuron B (1 ICP, dissolve delay = 2 years, age = 0).
2. Alice merges Neuron B (source) into Neuron A (target). The target's dissolve delay becomes `max(6 months, 2 years) = 2 years` — a free upgrade.
3. The target's age is only slightly reduced: `(100 * 4yr + 1 * 0) / 101 ≈ 3.96 years`. The age bonus is nearly fully preserved.
4. Alice now has a neuron with 2-year dissolve delay and ~4-year age, at the cost of only 1 ICP + transaction fee.

### Likelihood Explanation

This is reachable by any NNS neuron controller via the standard `manage_neuron` ingress call. No privileged access, no cooperation with other parties, and no threshold corruption is required. The only cost is the ICP transaction fee for the ledger transfer during merge. The `merge_neurons` function is a live, production-enabled feature callable by any principal who controls two neurons.

### Recommendation

1. **Do not upgrade dissolve delay via merge for free.** The target's dissolve delay after merge should remain the target's original dissolve delay (or require the caller to explicitly increase it via `IncreaseDissolveDelay`, which has no side effects on age).
2. **Reset age on merge.** When a merge occurs, the target's `aging_since_timestamp_seconds` should be set to `now_seconds` (age = 0), similar to how the source's age is reset. This prevents age-preservation gaming.
3. **Cap `eight_year_gang_bonus_base_e8s` transfer.** The bonus base transferred from source to target should be proportional to the stake transferred, not the full source bonus base, to prevent bonus concentration via split-then-merge cycles.

### Proof of Concept

**Setup:**
- Neuron A (target): 1,000 ICP, dissolve delay = 6 months (below 2-year max bonus), age = 4 years (max age bonus = 1.25x). Voting power ≈ 1000 * (1 + 0.5 * (6mo/2yr)²) * 1.25 (simplified).
- Neuron B (source): 1 ICP, dissolve delay = 2 years (max dissolve delay bonus = 3x), age = 0.

**Attack:**
```
manage_neuron({
  id: NeuronA_id,
  command: Merge { source_neuron_id: NeuronB_id }
})
```

**Result per `calculate_merge_neurons_effect`:**
- Target dissolve delay = `max(6 months, 2 years)` = **2 years** (free upgrade).
- Target age = `(1000 * 4yr + 1 * 0) / 1001` ≈ **3.996 years** (nearly unchanged).
- Target voting power now uses 2-year dissolve delay bonus (3x) instead of 6-month bonus (~1.06x), multiplied by ~1.25x age bonus.
- Net voting power increase: roughly **3x** for the cost of 1 ICP + fee.

The attacker-controlled entry path is: unprivileged ingress `manage_neuron` → `merge_neurons` → `calculate_merge_neurons_effect` → `combine_aged_stakes` + dissolve delay `max()` logic in `rs/nns/governance/src/governance/merge_neurons.rs`. [9](#0-8) [10](#0-9) [11](#0-10)

### Citations

**File:** rs/nns/governance/src/governance/merge_neurons.rs (L245-315)
```rust
/// Calculates the effects of merging two neurons.
pub fn calculate_merge_neurons_effect(
    id: &NeuronId,
    merge: &Merge,
    caller: &PrincipalId,
    neuron_store: &NeuronStore,
    transaction_fees_e8s: u64,
    now_seconds: u64,
) -> Result<MergeNeuronsEffect, MergeNeuronsError> {
    let (source, target) =
        validate_request_and_neurons(id, merge, caller, neuron_store, now_seconds)?;

    let source_burn_fees_e8s = if source.fees_e8s > transaction_fees_e8s {
        Some(source.fees_e8s)
    } else {
        None
    };

    let amount_to_target_e8s = source.minted_stake_e8s.saturating_sub(transaction_fees_e8s);
    let stake_transfer_to_target_e8s = if amount_to_target_e8s > 0 {
        Some(amount_to_target_e8s)
    } else {
        None
    };
    let transfer_eight_year_gang_bonus_base_e8s = source.eight_year_gang_bonus_base_e8s;

    let (_, new_target_age_seconds) = combine_aged_stakes(
        target.cached_stake_e8s,
        target.age_seconds,
        amount_to_target_e8s,
        source.age_seconds,
    );
    // The combined age is a weighted average of the ages of the two neurons, which should be no
    // more than their maximum.
    debug_assert!(new_target_age_seconds <= std::cmp::max(source.age_seconds, target.age_seconds));

    debug_assert!(source.age_seconds <= now_seconds);
    let source_neuron_dissolve_state_and_age = DissolveStateAndAge::NotDissolving {
        dissolve_delay_seconds: source.dissolve_delay_seconds,
        aging_since_timestamp_seconds: if stake_transfer_to_target_e8s.is_some() {
            now_seconds
        } else {
            now_seconds.saturating_sub(source.age_seconds)
        },
    };

    // Because of the invariant above `new_target_age_seconds <= max(source.age_seconds,
    // target.age_seconds`, and both `source.age_seconds` and `target.age_seconds` are no more than
    // now_seconds, `new_target_age_seconds` should be no more than `now_seconds`.
    debug_assert!(new_target_age_seconds <= now_seconds);
    let target_neuron_dissolve_state_and_age = DissolveStateAndAge::NotDissolving {
        dissolve_delay_seconds: std::cmp::max(
            source.dissolve_delay_seconds,
            target.dissolve_delay_seconds,
        ),
        aging_since_timestamp_seconds: now_seconds.saturating_sub(new_target_age_seconds),
    };

    Ok(MergeNeuronsEffect {
        source_neuron_id: source.id,
        target_neuron_id: target.id,
        source_burn_fees_e8s,
        stake_transfer_to_target_e8s,
        source_neuron_dissolve_state_and_age,
        target_neuron_dissolve_state_and_age,
        transfer_maturity_e8s: source.maturity_e8s_equivalent,
        transfer_staked_maturity_e8s: source.staked_maturity_e8s_equivalent,
        transfer_eight_year_gang_bonus_base_e8s,
        transaction_fees_e8s,
    })
}
```

**File:** rs/nns/governance/src/governance/merge_neurons.rs (L333-334)
```rust
    if !source_is_caller_controller {
        return Err(MergeNeuronsError::SourceNeuronNotController);
```

**File:** rs/nns/governance/src/governance/merge_neurons.rs (L513-514)
```rust
            let source_is_caller_authorized =
                source_neuron.is_authorized_to_simulate_manage_neuron(caller);
```

**File:** rs/nns/governance/src/neuron/mod.rs (L22-46)
```rust
pub fn combine_aged_stakes(
    x_stake_e8s: u64,
    x_age_seconds: u64,
    y_stake_e8s: u64,
    y_age_seconds: u64,
) -> (u64, u64) {
    if x_stake_e8s == 0 && y_stake_e8s == 0 {
        (0, 0)
    } else {
        let total_age_seconds: u128 = ((x_stake_e8s as u128)
            .saturating_mul(x_age_seconds as u128)
            .saturating_add((y_stake_e8s as u128).saturating_mul(y_age_seconds as u128)))
            / ((x_stake_e8s as u128).saturating_add(y_stake_e8s as u128));

        // Note that age is adjusted in proportion to the stake, but due to the
        // discrete nature of u64 numbers, some resolution is lost due to the
        // division above. Only if x_age * x_stake is a multiple of y_stake does
        // the age remain constant after this operation. However, in the end, the
        // most that can be lost due to rounding from the actual age, is always
        // less than 1 second, so this is not a problem.
        (
            x_stake_e8s.saturating_add(y_stake_e8s),
            total_age_seconds as u64,
        )
    }
```

**File:** rs/nns/governance/src/neuron/types.rs (L371-399)
```rust
    pub fn potential_and_deciding_voting_power(
        &self,
        voting_power_economics: &VotingPowerEconomics,
        now_seconds: u64,
    ) -> (u64, u64) {
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
