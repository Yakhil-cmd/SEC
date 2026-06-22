### Title
NNS Neuron Following Cleared Without Grace Period After Subnet Halt-and-Recovery Time Jump - (File: `rs/nns/governance/src/neuron_store.rs`)

---

### Summary

The NNS governance canister implements a "periodic confirmation of following" mechanism (`VotingPowerEconomics`) that clears neuron following when a neuron has not refreshed its voting power within a fixed staleness window (default: 6 months + 1 month = 7 months). When the NNS subnet is halted for maintenance and subsequently recovered via a recovery CUP, the subnet's block time jumps forward to the value specified in the recovery proposal. The `prune_some_following` timer task then runs immediately with the new (jumped) time, and any neurons whose staleness window expired during the halt have their following cleared instantly — with no grace period for neuron owners to take action, since they were unable to interact with the canister during the halt.

---

### Finding Description

The NNS governance canister tracks neuron activity via `voting_power_refreshed_timestamp_seconds`. The `prune_following` method in `rs/nns/governance/src/neuron/types.rs` computes staleness as:

```rust
let is_fresh = self.voting_power_refreshed_timestamp_seconds
    >= now_seconds
        .saturating_sub(voting_power_economics.get_start_reducing_voting_power_after_seconds())
        .saturating_sub(voting_power_economics.get_clear_following_after_seconds());
``` [1](#0-0) 

If `is_fresh` is false, all followees except `NeuronManagement` are cleared immediately:

```rust
self.followees.retain(|topic, _| *topic == Topic::NeuronManagement as i32);
``` [2](#0-1) 

This is called by `prune_some_following`, a timer task that runs periodically: [3](#0-2) 

The default staleness window is `start_reducing_voting_power_after_seconds` = 6 months + `clear_following_after_seconds` = 1 month: [4](#0-3) 

The NNS subnet can be halted via a `SetSubnetOperationalLevel` governance proposal (setting `is_halted = true` in `SubnetRecord`): [5](#0-4) 

When halted, no blocks are produced and no messages are processed. Neuron owners cannot call `manage_neuron` to refresh their voting power. When the subnet is recovered, the recovery CUP specifies a new time greater than the halt time: [6](#0-5) 

The governance canister's `env.now()` then returns this jumped time. The `prune_some_following` timer fires in the first round after recovery and immediately clears following for any neuron whose staleness window expired during the halt — without any grace period.

---

### Impact Explanation

**Impact: Medium-High.** Neuron owners who were within the final month of their staleness window at the time of the halt have their following cleared the moment the subnet resumes. They:

1. Lose all governance following configuration (except `NeuronManagement`), which they set up deliberately.
2. Lose voting rewards for all proposals that pass while their following is cleared and before they can reconfigure it.
3. Had no ability to take any action during the halt to prevent this outcome.

For large neuron holders, voting rewards represent significant ICP value. The loss is irreversible for the reward period during which following was absent.

---

### Likelihood Explanation

**Likelihood: Low.** Two conditions must coincide:

1. The NNS subnet is halted for maintenance (a real but rare operational event — the `halt_subnet` / `unhalt_subnet` procedures exist and have been used).
2. A neuron's `voting_power_refreshed_timestamp_seconds` is within the `clear_following_after_seconds` window (1 month) of the staleness cutoff at the time of the halt.

The NNS subnet has been halted before for key resharing and recovery operations. Neurons that have been inactive for 6+ months are common on the NNS. The combination is low-probability but not theoretical.

---

### Recommendation

After a subnet halt-and-recovery, add a post-resumption grace period before `prune_some_following` clears any following. This can be implemented by:

1. Recording the time of the last subnet recovery in governance state.
2. In `prune_following`, extending the staleness cutoff by a grace period (e.g., 2 weeks) if the current time is within that grace period of the last recovery.

Alternatively, `prune_some_following` could skip clearing following for neurons whose staleness window would not have expired had the subnet not been halted (i.e., compare against the halt time, not the recovery time).

---

### Proof of Concept

1. Neuron owner last refreshed voting power exactly 6 months and 25 days ago (`voting_power_refreshed_timestamp_seconds = T - 6mo25d`).
2. NNS governance passes a `SetSubnetOperationalLevel` proposal setting `is_halted = true`.
3. The subnet halts; no blocks are produced for 6 days.
4. A recovery proposal is submitted specifying a new CUP time `T + 6d` (reflecting elapsed real-world time).
5. The subnet resumes. The governance canister's `env.now()` returns `T + 6d`.
6. The `prune_some_following` timer fires. For the neuron: `now_seconds - voting_power_refreshed_timestamp_seconds = 6mo25d + 6d = 7mo1d > 7mo`.
7. `is_fresh` evaluates to `false`; all non-`NeuronManagement` followees are cleared immediately.
8. The neuron owner had no opportunity to call `manage_neuron` during the halt to prevent this. [7](#0-6) [8](#0-7) [3](#0-2) [9](#0-8)

### Citations

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

**File:** rs/nns/governance/src/network_economics.rs (L263-298)
```rust
    pub const DEFAULT: Self = Self {
        start_reducing_voting_power_after_seconds: Some(
            Self::DEFAULT_START_REDUCING_VOTING_POWER_AFTER_SECONDS,
        ),
        clear_following_after_seconds: Some(Self::DEFAULT_CLEAR_FOLLOWING_AFTER_SECONDS),
        neuron_minimum_dissolve_delay_to_vote_seconds: Some(
            Self::DEFAULT_NEURON_MINIMUM_DISSOLVE_DELAY_TO_VOTE_SECONDS,
        ),
    };

    /// Only neurons with at least this dissolve delay may submit proposals.
    ///
    /// When a proposal is created, neurons with dissolve delay (in seconds) less than
    /// `VotingPowerEconomics.min_dissolve_delay_seconds` receive no ballot (to be filled out)
    /// for that proposal. Thus, such neurons cannot vote on the proposal.
    pub const DEFAULT_NEURON_MINIMUM_DISSOLVE_DELAY_TO_VOTE_SECONDS: u64 = 6 * ONE_MONTH_SECONDS;

    /// The default value for `neuron_minimum_dissolve_delay_to_vote_seconds` once the mission 70
    /// voting rewards feature is enabled. Two weeks instead of six months.
    pub const MISSION_70_DEFAULT_NEURON_MINIMUM_DISSOLVE_DELAY_TO_VOTE_SECONDS: u64 =
        14 * ONE_DAY_SECONDS;

    /// A proposal to set `VotingPowerEconomics.min_dissolve_delay_seconds` must specify a value
    /// for this field that falls within this range. Changing the lower bound of this parameter
    /// requires manually checking how it might interact with other aspects of the NNS.
    /// In particular, it is not currently possible for a dissolved neuron to cast a vote, as
    /// the minimal dissolve delay to be eligible for voting exceeds the maximal voting period.
    /// Thus, there may be implicit dependencies of the NNS itself or its clients on this aspect,
    /// which originate from the time when the minimum dissolve delay to vote was an internal NNS
    /// constant.
    pub const NEURON_MINIMUM_DISSOLVE_DELAY_TO_VOTE_SECONDS_BOUNDS: RangeInclusive<u64> =
        (14 * ONE_DAY_SECONDS)..=(6 * ONE_MONTH_SECONDS);

    pub const DEFAULT_START_REDUCING_VOTING_POWER_AFTER_SECONDS: u64 = 6 * ONE_MONTH_SECONDS;

    pub const DEFAULT_CLEAR_FOLLOWING_AFTER_SECONDS: u64 = ONE_MONTH_SECONDS;
```

**File:** rs/protobuf/def/registry/subnet/v1/subnet.proto (L38-39)
```text
  // If `true`, the subnet will be halted: it will no longer create or execute blocks.
  bool is_halted = 17;
```

**File:** rs/cup_explorer/README.md (L91-92)
```markdown
A recovery proposal should specify a time and height that is greater than the time and height of the CUP above.
Additionally, the proposed state hash should be equal to the one in the provided CUP, to ensure there were no modifications to the state.
```
