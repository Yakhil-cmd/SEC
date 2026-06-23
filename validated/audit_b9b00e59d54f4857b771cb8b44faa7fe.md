### Title
NNS Governance `ClaimOrRefresh` Stake Top-Up Does Not Refresh `voting_power_refreshed_timestamp_seconds`, Silently Reducing `deciding_voting_power` and Voting Rewards for Honest Stakers - (File: `rs/nns/governance/src/governance.rs`)

---

### Summary

The NNS Governance `deciding_voting_power` mechanism (Mission 70 / Proposal 132411) is designed to encourage active participation by linearly reducing a neuron's effective voting power to zero if `voting_power_refreshed_timestamp_seconds` has not been updated in the last 6 months, and then clearing all non-ManageNeuron followees after a further 1 month. However, when a neuron owner tops up their stake via `ClaimOrRefresh` (the standard "increase position" action), the `refresh_neuron()` function updates the stake but **never** updates `voting_power_refreshed_timestamp_seconds`. An honest staker who has been inactive for >6 months and then tops up their stake will silently continue to receive reduced (or zero) voting rewards and will eventually lose their carefully configured following, without any indication that a separate `RefreshVotingPower` call is required.

---

### Finding Description

**The `deciding_voting_power` reduction mechanism**

`VotingPowerEconomics` defines two thresholds:
- `start_reducing_voting_power_after_seconds` (default: 6 months): after this time without a refresh, `deciding_voting_power` begins decreasing linearly toward 0.
- `clear_following_after_seconds` (default: 1 month): after a further month, `deciding_voting_power` reaches 0 and all non-ManageNeuron followees are cleared.

The adjustment factor is computed in `potential_and_deciding_voting_power()`:

```rust
let time_since_last_refreshed = Duration::from_secs(
    now_seconds.saturating_sub(self.voting_power_refreshed_timestamp_seconds),
);
voting_power_economics
    .deciding_voting_power_adjustment_factor(time_since_last_refreshed)
```

**The root cause: `refresh_neuron()` never calls `refresh_voting_power()`**

When a user tops up their stake by transferring ICP to the neuron's subaccount and calling `ClaimOrRefresh`, the governance canister routes to `refresh_neuron()`:

```rust
Ordering::Less => {
    neuron.update_stake_adjust_age(balance.get_e8s(), now);
}
```

`update_stake_adjust_age()` updates `cached_neuron_stake_e8s` and adjusts the age-weighted timestamp, but **never touches `voting_power_refreshed_timestamp_seconds`**. The only function that updates that field is `refresh_voting_power()`:

```rust
pub(crate) fn refresh_voting_power(&mut self, now_seconds: u64) {
    self.voting_power_refreshed_timestamp_seconds = now_seconds;
}
```

This function is only called by the explicit `RefreshVotingPower` command, by `set_following`, and by direct voting — not by `ClaimOrRefresh`.

**Three concrete harms to honest users (direct analog to the HMX report)**

1. **Silently reduced voting rewards.** A neuron owner who has been inactive for >6 months and tops up their stake (expecting to increase their governance influence) will still have a reduced `deciding_voting_power`. Since voting rewards are proportional to `deciding_voting_power` (not `potential_voting_power`), the user receives fewer rewards than their new, larger stake would suggest. They may not realize a separate `RefreshVotingPower` call is needed.

2. **Following cleared without warning.** If the user's last refresh was >7 months ago and they top up their stake without calling `RefreshVotingPower`, the periodic `prune_following()` timer task will clear all non-ManageNeuron followees. The user loses their carefully configured following setup — an irreversible loss of governance configuration — even though they just actively engaged with the protocol by adding more stake.

3. **Inability to benefit from the increased stake.** The user's `deciding_voting_power` (the value that actually counts for proposal ballots and reward distribution) remains at the reduced/zero level despite the larger stake. The user's increased ICP commitment yields no additional governance influence until they discover and call `RefreshVotingPower`.

---

### Impact Explanation

- **Governance authorization bug / ledger conservation bug**: Honest stakers who top up their stake receive fewer voting rewards than their stake entitles them to. Over the 1-month reduction window, a staker who added ICP at the 6-month mark could lose up to 100% of the voting reward increment from their new stake.
- **Irreversible following loss**: Once `prune_following()` clears followees, the user must manually reconfigure all their following relationships. This is a permanent loss of governance configuration state.
- **Misleading UX**: The `ClaimOrRefresh` operation succeeds and updates the stake, giving no indication that a second transaction (`RefreshVotingPower`) is required to restore full voting power. The user's `potential_voting_power` (visible in `get_neuron_info`) reflects the new stake, but `deciding_voting_power` (the value that actually matters) remains reduced — a discrepancy that is not surfaced to the user at the time of the top-up.

---

### Likelihood Explanation

- **Medium-high.** Any NNS neuron owner who has not voted directly, set following, or called `RefreshVotingPower` in the past 6 months and then tops up their stake via `ClaimOrRefresh` is affected. This is a common pattern: long-term passive stakers who rely on following for governance participation and periodically add ICP to their neuron. The `ClaimOrRefresh` path is the standard, documented way to increase a neuron's stake (used by Rosetta, the NNS dapp, and direct ledger transfers). There is no on-chain warning or error at the time of the top-up.

---

### Recommendation

`refresh_neuron()` should call `neuron.refresh_voting_power(now)` when the stake is successfully increased (the `Ordering::Less` branch). This mirrors the fix described in the HMX report: when a user legitimately increases their position, the protocol should not silently penalize them by leaving the anti-exploitation timer unchanged. Alternatively, the `ClaimOrRefresh` response should explicitly surface the current `deciding_voting_power` and `voting_power_refreshed_timestamp_seconds` so the user is aware they need to call `RefreshVotingPower` separately.

---

### Proof of Concept

**Entry path (unprivileged ingress sender):**

1. User has a neuron with `voting_power_refreshed_timestamp_seconds` set to 7 months ago (deciding_voting_power = 0, following about to be cleared).
2. User transfers additional ICP to the neuron's subaccount on the ICP ledger.
3. User calls `manage_neuron` with `Command::ClaimOrRefresh { by: By::MemoAndController(...) }` — a standard, permissionless ingress call.
4. Governance routes to `refresh_neuron()` → `update_stake_adjust_age()`. Stake is updated. `voting_power_refreshed_timestamp_seconds` is **not** updated.
5. The periodic `prune_following()` timer fires and clears all non-ManageNeuron followees (since `voting_power_refreshed_timestamp_seconds` is still 7 months ago).
6. The user's `deciding_voting_power` remains 0 despite the larger stake. They receive 0 voting rewards from following until they discover and call `RefreshVotingPower`.

**Relevant code locations:**

- `refresh_neuron()` — missing `refresh_voting_power()` call: [1](#0-0) 

- `update_stake_adjust_age()` — only updates stake and age, not voting power timestamp: [2](#0-1) 

- `refresh_voting_power()` — the function that is NOT called: [3](#0-2) 

- `deciding_voting_power` calculation using `voting_power_refreshed_timestamp_seconds`: [4](#0-3) 

- `prune_following()` — clears followees when timestamp is stale: [5](#0-4) 

- `VotingPowerEconomics` defaults (6-month and 1-month thresholds): [6](#0-5)

### Citations

**File:** rs/nns/governance/src/governance.rs (L5950-5951)
```rust
                Ordering::Less => {
                    neuron.update_stake_adjust_age(balance.get_e8s(), now);
```

**File:** rs/nns/governance/src/neuron/types.rs (L390-399)
```rust
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

**File:** rs/nns/governance/src/neuron/types.rs (L528-558)
```rust
    pub(crate) fn prune_following(
        &mut self,
        voting_power_economics: &VotingPowerEconomics,
        now_seconds: u64,
    ) -> u64 {
        let is_fresh = self.voting_power_refreshed_timestamp_seconds
            >= now_seconds
                .saturating_sub(
                    voting_power_economics.get_start_reducing_voting_power_after_seconds(),
                )
                .saturating_sub(voting_power_economics.get_clear_following_after_seconds());
        if is_fresh {
            return 0;
        }

        let mut result = 0_usize;
        for (topic, followees) in &self.followees {
            if *topic == Topic::NeuronManagement as i32 {
                continue;
            }
            result = result.saturating_add(followees.followees.len());
        }

        // Clear all following except ManageNeuron.
        self.followees
            .retain(|topic, _| *topic == Topic::NeuronManagement as i32);

        // If this panics, that means we somehow have around 2^64 (or more)
        // followees, which is not only disallowed, but just way more than we
        // would ever be able to hold in memory.
        u64::try_from(result).unwrap()
```

**File:** rs/nns/governance/src/neuron/types.rs (L999-1039)
```rust
    pub fn update_stake_adjust_age(&mut self, updated_stake_e8s: u64, now: u64) {
        // If the updated stake is less than the original stake, preserve the
        // age and distribute it over the new amount. This should not happen
        // in practice, so this code exists merely as a defensive fallback.
        //
        // TODO(NNS1-954) Consider whether update_stake_adjust_age (and other
        // similar methods) should use a neurons effective stake rather than
        // the cached stake.
        if updated_stake_e8s < self.cached_neuron_stake_e8s {
            println!(
                "{}Reducing neuron {:?} stake via update_stake_adjust_age: {} -> {}",
                LOG_PREFIX,
                self.id(),
                self.cached_neuron_stake_e8s,
                updated_stake_e8s
            );
            self.cached_neuron_stake_e8s = updated_stake_e8s;
        } else {
            // If one looks at "stake * age" as describing an area, the goal
            // at this point is to increase the stake while keeping the area
            // constant. This means decreasing the age in proportion to the
            // additional stake, which is the purpose of combine_aged_stakes.
            let (new_stake_e8s, new_age_seconds) = combine_aged_stakes(
                self.cached_neuron_stake_e8s,
                self.age_seconds(now),
                updated_stake_e8s.saturating_sub(self.cached_neuron_stake_e8s),
                0,
            );
            // A consequence of the math above is that the 'new_stake_e8s' is
            // always the same as the 'updated_stake_e8s'. We use
            // 'combine_aged_stakes' here to make sure the age is
            // appropriately pro-rated to accommodate the new stake.
            assert!(new_stake_e8s == updated_stake_e8s);
            self.cached_neuron_stake_e8s = new_stake_e8s;

            let new_aging_since_timestamp_seconds = now.saturating_sub(new_age_seconds);
            let new_disolved_dissolve_state_and_age = self
                .dissolve_state_and_age()
                .adjust_age(new_aging_since_timestamp_seconds);
            self.set_dissolve_state_and_age(new_disolved_dissolve_state_and_age);
        }
```

**File:** rs/nns/governance/src/network_economics.rs (L296-298)
```rust
    pub const DEFAULT_START_REDUCING_VOTING_POWER_AFTER_SECONDS: u64 = 6 * ONE_MONTH_SECONDS;

    pub const DEFAULT_CLEAR_FOLLOWING_AFTER_SECONDS: u64 = ONE_MONTH_SECONDS;
```
