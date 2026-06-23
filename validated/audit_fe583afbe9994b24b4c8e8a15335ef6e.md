### Title
NNS Governance `VotingPowerEconomics` Parameter Change Silently Degrades Existing Neuron Voting Power and Following Without User Warning - (`rs/nns/governance/src/network_economics.rs`)

### Summary

The NNS Governance canister allows the NNS DAO to change `VotingPowerEconomics` parameters (`start_reducing_voting_power_after_seconds`, `clear_following_after_seconds`, `neuron_minimum_dissolve_delay_to_vote_seconds`) via a `ManageNetworkEconomics` proposal. When these parameters are tightened (e.g., `start_reducing_voting_power_after_seconds` is reduced), all existing neurons that were previously "fresh" can instantly become "stale," causing their `deciding_voting_power` to drop to zero and their following to be cleared — with no on-chain warning, no grace period, and no documentation of the side effects on existing stakers. This is the direct IC analog of the Morpho LTV=0 issue: a governance-controlled parameter change silently and immediately degrades user positions in a way that is undocumented and unexpected.

### Finding Description

The `VotingPowerEconomics` struct in `rs/nns/governance/src/network_economics.rs` contains three governance-adjustable parameters:

1. `start_reducing_voting_power_after_seconds` — after this time without a refresh, deciding voting power begins to linearly decrease.
2. `clear_following_after_seconds` — after this additional time, deciding voting power reaches 0 and all non-NeuronManagement following is cleared.
3. `neuron_minimum_dissolve_delay_to_vote_seconds` — neurons below this dissolve delay are excluded from voting entirely.

These parameters are applied in `deciding_voting_power_adjustment_factor_function()`:

```rust
fn deciding_voting_power_adjustment_factor_function(&self) -> LinearMap {
    let from_range = {
        let begin = self.get_start_reducing_voting_power_after_seconds();
        let end = begin.saturating_add(self.get_clear_following_after_seconds());
        begin..end
    };
    let to_range = 1..0;
    LinearMap::new(from_range, to_range)
}
```

And in `compute_voting_power_snapshot_for_standard_proposal()`:

```rust
if neuron.is_inactive(now_seconds)
    || neuron.dissolve_delay_seconds(now_seconds) < min_dissolve_delay_seconds
{
    return;
}
```

The `validate()` method for `VotingPowerEconomics` explicitly states: *"They are allowed to be set to 0 though."* — meaning a governance proposal can set `start_reducing_voting_power_after_seconds = 0`, which would cause **all neurons** to immediately have their deciding voting power begin decaying, regardless of when they last refreshed.

Similarly, if `neuron_minimum_dissolve_delay_to_vote_seconds` is increased (within the allowed range of 14 days to 6 months), neurons with dissolve delays between the old and new threshold are instantly stripped of all voting power and rewards eligibility, with no grace period.

The `prune_following()` function in `rs/nns/governance/src/neuron/types.rs` uses the same parameters to determine whether to clear a neuron's following:

```rust
let is_fresh = self.voting_power_refreshed_timestamp_seconds
    >= now_seconds
        .saturating_sub(voting_power_economics.get_start_reducing_voting_power_after_seconds())
        .saturating_sub(voting_power_economics.get_clear_following_after_seconds());
```

A reduction in `start_reducing_voting_power_after_seconds + clear_following_after_seconds` can retroactively make previously-fresh neurons appear stale, triggering irreversible following deletion.

The `validate()` function does **not** check for minimum positive values on `start_reducing_voting_power_after_seconds` or `clear_following_after_seconds`, and there is no code that warns users, provides a grace period, or documents the side effects of parameter changes on existing neuron positions.

### Impact Explanation

When the NNS DAO passes a `ManageNetworkEconomics` proposal that tightens `VotingPowerEconomics`:

- **Deciding voting power drops to zero** for all neurons that have not refreshed within the new (shorter) window — immediately, at the next proposal creation. Affected neurons lose all voting rewards.
- **Following is irreversibly cleared** for all neurons that fall outside the new staleness window — this is a permanent, non-recoverable state change to user-owned neurons.
- **Neurons below the new `neuron_minimum_dissolve_delay_to_vote_seconds`** are instantly excluded from voting and receive no ballots on future proposals, losing all voting rewards until they increase their dissolve delay (which requires locking ICP for longer).
- Users who staked ICP under the assumption of a certain refresh window or minimum dissolve delay threshold are harmed without any on-chain warning or grace period.

This is a governance authorization / user-position-impact bug: the protocol's internal accounting diverges from user expectations in a way that harms users, directly analogous to Morpho's LTV=0 handling.

### Likelihood Explanation

The NNS DAO has already demonstrated willingness to change these parameters: Mission 70 (Proposal 141380 area) reduced `neuron_minimum_dissolve_delay_to_vote_seconds` from 6 months to 2 weeks, and the `start_reducing_voting_power_after_seconds` / `clear_following_after_seconds` parameters are actively used and adjustable. Any future `ManageNetworkEconomics` proposal that tightens these values — even by a small amount — will silently harm existing neuron holders. The entry path is a standard NNS governance proposal, executable by any neuron with sufficient voting power, with no special privilege required beyond normal NNS majority.

### Recommendation

1. **Add validation bounds** on `start_reducing_voting_power_after_seconds` and `clear_following_after_seconds` to prevent them from being set to zero or to values that would retroactively invalidate all existing neurons.
2. **Document the side effects** of parameter changes on existing neuron positions in the `VotingPowerEconomics` struct and in the `ManageNetworkEconomics` proposal execution path.
3. **Implement a grace period** mechanism: when these parameters are tightened, neurons should have a transition window (e.g., equal to the old value) before the new stricter rules apply to their existing `voting_power_refreshed_timestamp_seconds`.
4. **Emit on-chain events or metrics** when a parameter change would affect a significant fraction of existing neurons, so that users and dashboards can warn stakers.
5. **Document this behavior** in the NNS governance documentation and dashboard, analogous to the Morpho recommendation.

### Proof of Concept

**Step 1:** NNS DAO passes a `ManageNetworkEconomics` proposal setting:
```
start_reducing_voting_power_after_seconds = 42  // ~42 seconds instead of 6 months
clear_following_after_seconds = 58              // ~58 seconds instead of 1 month
```

This is accepted by `validate()` because the comment explicitly states *"They are allowed to be set to 0 though."* [1](#0-0) 

**Step 2:** `perform_manage_network_economics_impl()` applies the change immediately with no grace period: [2](#0-1) 

**Step 3:** At the next proposal creation, `compute_voting_power_snapshot_for_standard_proposal()` calls `potential_and_deciding_voting_power()` for every neuron. For any neuron that last refreshed more than 42 seconds ago (i.e., virtually all neurons), `deciding_voting_power_adjustment_factor()` returns 0: [3](#0-2) 

**Step 4:** The periodic `prune_some_following` task calls `prune_following()` on every neuron. Since `start_reducing_voting_power_after_seconds + clear_following_after_seconds = 100 seconds`, any neuron that last refreshed more than 100 seconds ago has its following irreversibly cleared: [4](#0-3) 

**Step 5:** Neurons with dissolve delay between the old and new `neuron_minimum_dissolve_delay_to_vote_seconds` are excluded from all future ballots: [5](#0-4) 

The test `test_prune_some_following_super_strict_voting_power_refresh` in the codebase explicitly demonstrates that setting small values for these parameters causes all neurons to have their following cleared: [6](#0-5)

### Citations

**File:** rs/nns/governance/src/network_economics.rs (L315-336)
```rust
    pub fn deciding_voting_power_adjustment_factor(
        &self,
        time_since_last_voting_power_refreshed: Duration,
    ) -> Decimal {
        self.deciding_voting_power_adjustment_factor_function()
            .apply(time_since_last_voting_power_refreshed.as_secs())
            .clamp(Decimal::from(0), Decimal::from(1))
    }

    fn deciding_voting_power_adjustment_factor_function(&self) -> LinearMap {
        let from_range = {
            let begin = self.get_start_reducing_voting_power_after_seconds();
            let end = begin.saturating_add(self.get_clear_following_after_seconds());

            begin..end
        };

        #[allow(clippy::reversed_empty_ranges)]
        let to_range = 1..0;

        LinearMap::new(from_range, to_range)
    }
```

**File:** rs/nns/governance/src/network_economics.rs (L348-351)
```rust
    /// This just validates that all fields are set.
    ///
    /// They are allowed to be set to 0 though.
    ///
```

**File:** rs/nns/governance/src/governance.rs (L4298-4317)
```rust
    fn perform_manage_network_economics_impl(
        &mut self,
        proposed_network_economics: NetworkEconomics,
    ) -> Result<(), GovernanceError> {
        let new_network_economics = self
            .economics()
            .apply_changes_and_validate(&proposed_network_economics)
            .map_err(|defects| {
                GovernanceError::new_with_message(
                    ErrorType::InvalidProposal,
                    format!(
                        "The resulting NetworkEconomics is invalid for the following reason(s):\
                         \n  - {}",
                        defects.join("\n  - "),
                    ),
                )
            })?;

        self.heap_data.economics = Some(new_network_economics);
        Ok(())
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

**File:** rs/nns/governance/src/neuron_store/voting_power.rs (L140-149)
```rust
        let min_dissolve_delay_seconds = voting_power_economics
            .neuron_minimum_dissolve_delay_to_vote_seconds
            .unwrap_or(default_min_dissolve_delay);

        let mut process_neuron = |neuron: &Neuron| {
            if neuron.is_inactive(now_seconds)
                || neuron.dissolve_delay_seconds(now_seconds) < min_dissolve_delay_seconds
            {
                return;
            }
```

**File:** rs/nns/governance/src/neuron_store/neuron_store_tests.rs (L646-661)
```rust
    assert_eq!(
        prune_some_following(
            &VotingPowerEconomics {
                // These are much smaller than the normal values. As a result, all
                // neurons suddenly look stale. As a result, all following is
                // supposed to be cleared.
                start_reducing_voting_power_after_seconds: Some(42),
                clear_following_after_seconds: Some(58),
                neuron_minimum_dissolve_delay_to_vote_seconds: Some(42)
            },
            &mut neuron_store,
            Bound::Unbounded, // Start new cycle.
            || true,          // Do a full cycle.
        ),
        Bound::Unbounded,
    );
```
