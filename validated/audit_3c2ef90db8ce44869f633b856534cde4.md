### Title
Changing `VotingPowerEconomics` Parameters Retroactively Clears Following of Neurons That Refreshed Under Old Parameters - (File: rs/nns/governance/src/neuron/types.rs)

### Summary
The NNS governance `prune_following` function computes neuron "freshness" dynamically using the **current** `VotingPowerEconomics` parameters at the time of pruning, not the parameters that were in effect when the neuron last refreshed. A `ManageNetworkEconomics` proposal that reduces `start_reducing_voting_power_after_seconds` or `clear_following_after_seconds` immediately and retroactively reclassifies neurons that refreshed within the old "safe" window as "stale," causing their following to be cleared without warning on the next background pruning pass.

### Finding Description
The `prune_following` method in `rs/nns/governance/src/neuron/types.rs` determines whether a neuron's following should be cleared by computing a freshness threshold from the **live** `VotingPowerEconomics` parameters:

```rust
let is_fresh = self.voting_power_refreshed_timestamp_seconds
    >= now_seconds
        .saturating_sub(
            voting_power_economics.get_start_reducing_voting_power_after_seconds(),
        )
        .saturating_sub(voting_power_economics.get_clear_following_after_seconds());
``` [1](#0-0) 

The `VotingPowerEconomics` struct holds `start_reducing_voting_power_after_seconds` (default 6 months) and `clear_following_after_seconds` (default 1 month), both of which are mutable via a `ManageNetworkEconomics` governance proposal. [2](#0-1) 

When a `ManageNetworkEconomics` proposal executes, `perform_manage_network_economics_impl` immediately replaces the live `NetworkEconomics` (and thus `VotingPowerEconomics`) with the new values: [3](#0-2) 

The background `PruneFollowingTask` runs every 10 seconds and calls `prune_some_following`, which passes the **current** `VotingPowerEconomics` to `prune_following` for every neuron: [4](#0-3) [5](#0-4) 

There is no per-neuron record of which parameter values were in effect at the time of the last refresh. The neuron only stores `voting_power_refreshed_timestamp_seconds`: [6](#0-5) 

### Impact Explanation
If `start_reducing_voting_power_after_seconds` is reduced from 6 months to 3 months via a governance proposal, any neuron that refreshed between 3 and 6 months ago — which was fully "fresh" and safe under the old parameters — is immediately reclassified as "stale." Within 10 seconds, the next pruning pass will:

1. Clear all followees on every topic except `NeuronManagement`, removing the neuron's ability to vote via following.
2. Reduce the neuron's `deciding_voting_power` to 0 (since the adjustment factor also uses the same parameters). [7](#0-6) 

This can affect a large fraction of the neuron population simultaneously, silently disrupting ongoing proposal voting and reward accrual for neuron holders who believed they had refreshed within the valid window.

### Likelihood Explanation
A `ManageNetworkEconomics` proposal to tighten the refresh window is a plausible governance action (e.g., to increase active participation). It requires NNS governance majority, but such proposals are a normal part of NNS operation. The harm to existing neurons is a side-effect of the parameter change, not its intent, and the protocol provides no warning or grace period to affected neuron holders. The test `test_prune_some_following_super_strict_voting_power_refresh` explicitly demonstrates that reducing these parameters immediately reclassifies previously-fresh neurons as stale: [8](#0-7) 

### Recommendation
Store the computed "safe-until" deadline at refresh time rather than recomputing it from live parameters at prune time. Concretely, add a `following_safe_until_timestamp_seconds` field to the neuron that is set to `now + start_reducing_voting_power_after_seconds + clear_following_after_seconds` when `refresh_voting_power` is called. The `prune_following` check then becomes:

```rust
let is_fresh = now_seconds < self.following_safe_until_timestamp_seconds;
```

This mirrors the BendDAO mitigation of storing the end timestamp rather than recomputing it from a mutable duration parameter. Alternatively, apply parameter reductions only to neurons that refresh after the change takes effect, by treating the new parameters as a floor rather than an immediate replacement.

### Proof of Concept
1. Neuron A calls `refresh_voting_power` at time `T = now − 4 months`. Its `voting_power_refreshed_timestamp_seconds = T`.
2. Current parameters: `start_reducing_voting_power_after_seconds = 6 months`, `clear_following_after_seconds = 1 month`. Freshness threshold = `now − 7 months`. Neuron A is fresh (`T > now − 7 months`). ✓
3. A `ManageNetworkEconomics` proposal passes, setting `start_reducing_voting_power_after_seconds = 2 months`, `clear_following_after_seconds = 1 month`. New threshold = `now − 3 months`.
4. Within 10 seconds, `PruneFollowingTask` fires. For Neuron A: `is_fresh = (now − 4 months) >= (now − 3 months)` → **false**.
5. `prune_following` clears all of Neuron A's followees (except `NeuronManagement`) immediately, without any notification to the neuron holder.
6. Neuron A's `deciding_voting_power` drops to 0 on the next proposal snapshot, silently removing its influence from any open proposals.

### Citations

**File:** rs/nns/governance/src/neuron/types.rs (L390-397)
```rust
        let adjustment_factor: Decimal = {
            let time_since_last_refreshed = Duration::from_secs(
                now_seconds.saturating_sub(self.voting_power_refreshed_timestamp_seconds),
            );

            voting_power_economics
                .deciding_voting_power_adjustment_factor(time_since_last_refreshed)
        };
```

**File:** rs/nns/governance/src/neuron/types.rs (L515-517)
```rust
    pub(crate) fn refresh_voting_power(&mut self, now_seconds: u64) {
        self.voting_power_refreshed_timestamp_seconds = now_seconds;
    }
```

**File:** rs/nns/governance/src/neuron/types.rs (L533-538)
```rust
        let is_fresh = self.voting_power_refreshed_timestamp_seconds
            >= now_seconds
                .saturating_sub(
                    voting_power_economics.get_start_reducing_voting_power_after_seconds(),
                )
                .saturating_sub(voting_power_economics.get_clear_following_after_seconds());
```

**File:** rs/nns/governance/src/network_economics.rs (L296-298)
```rust
    pub const DEFAULT_START_REDUCING_VOTING_POWER_AFTER_SECONDS: u64 = 6 * ONE_MONTH_SECONDS;

    pub const DEFAULT_CLEAR_FOLLOWING_AFTER_SECONDS: u64 = ONE_MONTH_SECONDS;
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

**File:** rs/nns/governance/src/neuron_store.rs (L967-991)
```rust
pub fn prune_some_following(
    voting_power_economics: &VotingPowerEconomics,
    neuron_store: &mut NeuronStore,
    next: Bound<NeuronId>,
    carry_on: impl FnMut() -> bool,
) -> Bound<NeuronId> {
    let now_seconds = neuron_store.now();

    if next == Bound::Unbounded {
        CURRENT_PRUNE_FOLLOWING_FULL_CYCLE_START_TIMESTAMP_SECONDS.with(
            |start_timestamp_seconds| {
                start_timestamp_seconds.set(now_seconds);
            },
        );
    }

    groom_some_neurons(
        neuron_store,
        |neuron| {
            neuron.prune_following(voting_power_economics, now_seconds);
        },
        next,
        carry_on,
    )
}
```

**File:** rs/nns/governance/src/timer_tasks/prune_following.rs (L43-57)
```rust
impl RecurringSyncTask for PruneFollowingTask {
    fn execute(self) -> (Duration, Self) {
        let new_begin = self.governance.with_borrow_mut(|governance| {
            let carry_on = || !is_message_over_threshold(MAX_PRUNE_SOME_FOLLOWING_INSTRUCTIONS);
            governance.prune_some_following(self.begin, carry_on)
        });

        (
            PRUNE_FOLLOWING_INTERVAL,
            Self {
                governance: self.governance,
                begin: new_begin,
            },
        )
    }
```

**File:** rs/nns/governance/src/neuron_store/neuron_store_tests.rs (L595-661)
```rust
/// This shows that VotingPowerEconomics is used when pruning following, not the
/// old constant(s).
#[test]
fn test_prune_some_following_super_strict_voting_power_refresh() {
    // Step 1: Prepare the world. (This is exactly the same as the previous test.)

    let followees = hashmap! {
        Topic::Governance as i32 => Followees {
            followees: vec![NeuronId { id: 99 }],
        },
        Topic::NeuronManagement as i32 => Followees {
            followees: vec![NeuronId { id: 101 }],
        },
    };

    let mut fresh_neuron = simple_neuron_builder(1)
        .with_followees(followees.clone())
        .build();
    fresh_neuron.refresh_voting_power(CREATED_TIMESTAMP_SECONDS - 7 * ONE_MONTH_SECONDS + 1);

    // Similar to fresh_neuron, except voting power was refrshed a "long" time
    // ago.
    let mut stale_neuron = simple_neuron_builder(3)
        .with_followees(followees.clone())
        .build();
    stale_neuron.refresh_voting_power(CREATED_TIMESTAMP_SECONDS - 7 * ONE_MONTH_SECONDS - 1);

    let mut neuron_store = NeuronStore::new(btreemap! {
        fresh_neuron.id().id => fresh_neuron.clone(),
        stale_neuron.id().id => stale_neuron.clone(),
    });

    // Control the perception of time by neuron_store.
    #[derive(Debug, Clone)]
    struct DummyClock {}
    impl Clock for DummyClock {
        fn now(&self) -> u64 {
            CREATED_TIMESTAMP_SECONDS
        }

        fn set_time_warp(&mut self, _: TimeWarp) {
            unimplemented!();
        }
    }
    impl PracticalClock for DummyClock {}
    let clock = DummyClock {};
    neuron_store.clock = Box::new(clock);

    // Step 2: Call code under test. (This is where things start looking
    // different, compared to the previous test.)

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
