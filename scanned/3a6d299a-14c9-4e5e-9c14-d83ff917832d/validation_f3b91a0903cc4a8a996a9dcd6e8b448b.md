### Title
Permanent Loss of ICP-Equivalent Maturity Due to Rounding Truncation in Voting Reward Distribution - (File: rs/nns/governance/src/governance.rs)

### Summary
In the NNS Governance canister's `calculate_voting_rewards` function, each neuron's reward is computed via floor-truncating integer division. The sum of all truncated per-neuron rewards is always strictly less than `total_available_e8s_equivalent`. The undistributed remainder is **permanently discarded** every reward round in which proposals are settled — it is never rolled over to the next event. This is the direct IC analog of the LOB `calcCommissions` rounding accumulation bug.

### Finding Description

In `calculate_voting_rewards` inside `rs/nns/governance/src/governance.rs`, each neuron's reward is computed as:

```rust
let reward = (used_voting_rights * total_available_e8s_equivalent_float
    / total_voting_rights) as u64;
```

The `as u64` cast truncates (floors) the floating-point result. The sum of all truncated rewards is accumulated in `actually_distributed_e8s_equivalent`. [1](#0-0) 

The `RewardEvent` records both `distributed_e8s_equivalent` (the truncated sum) and `total_available_e8s_equivalent` (the full pool). The difference — the undistributed dust — is never recovered. [2](#0-1) 

The rollover mechanism in `e8s_equivalent_to_be_rolled_over()` only rolls over the full `total_available_e8s_equivalent` when **no proposals were settled** (`settled_proposals.is_empty()`). When proposals are settled (the normal operating case), it returns `0` — meaning the rounding residual is silently dropped. [3](#0-2) 

The next round's available pool is built from the previous event's rollover plus the new supply fraction:

```rust
let total_available_e8s_equivalent_float = (supply.get_e8s() as f64) * fraction
    + rolling_over_from_previous_reward_event_e8s_equivalent as f64;
``` [4](#0-3) 

Since `rolling_over_from_previous_reward_event_e8s_equivalent` is `0` whenever proposals were settled, the rounding residual from the previous round is permanently lost.

The SNS Governance canister has the identical pattern in `distribute_rewards`, and even explicitly acknowledges it in a comment:

> "Because of rounding (and other shenanigans), it is possible that some portion of this amount ends up not being actually distributed." [5](#0-4) [6](#0-5) 

### Impact Explanation

Every reward round in which at least one proposal is settled, up to `(N − 1)` e8s of ICP-equivalent maturity are permanently destroyed, where N is the number of distinct neurons that voted. With thousands of active neurons on the NNS, this residual can reach thousands of e8s per day. Over years of continuous operation, the cumulative loss is non-trivial: at 10,000 voting neurons per round and daily rounds, up to ~3.65 million e8s (~0.0365 ICP) per year are silently discarded from the reward pool. The maturity is neither credited to any neuron nor rolled over — it simply vanishes from the accounting.

### Likelihood Explanation

This occurs on every single reward distribution round that settles at least one proposal, which is the normal operating condition of the NNS. No special conditions, attacker actions, or rare states are required. The governance canister's periodic task triggers this automatically.

### Recommendation

Compute the undistributed remainder as `total_available_e8s_equivalent - actually_distributed_e8s_equivalent` and roll it over into the next reward event's available pool, analogous to how the full amount is rolled over when no proposals are settled. Alternatively, assign the remainder to the last neuron in the distribution loop (the approach recommended in the external report for `admin_amount`).

### Proof of Concept

Consider a reward round with:
- `total_available_e8s_equivalent = 100` e8s
- 3 neurons with equal voting power (each gets `100 / 3 = 33` e8s via floor division)
- `actually_distributed_e8s_equivalent = 99`
- Residual = `1` e8s

Since `settled_proposals` is non-empty, `e8s_equivalent_to_be_rolled_over()` returns `0`. The 1 e8s residual is not added to the next round's pool. This matches the existing test behavior documented in `test_neuron_sometimes_active_sometimes_passive_which_proposal_does_not_matter`:

> "Thus a neuron that votes 3 times, receives 3×6.25 = 18.75 truncated to 18." [7](#0-6) 

The 4 e8s of residual (100 − 4×18 = 28 distributed vs. 100 available) are permanently lost each such round.

### Citations

**File:** rs/nns/governance/src/governance.rs (L6651-6654)
```rust
        let rolling_over_from_previous_reward_event_e8s_equivalent =
            latest_reward_event.e8s_equivalent_to_be_rolled_over();
        let total_available_e8s_equivalent_float = (supply.get_e8s() as f64) * fraction
            + rolling_over_from_previous_reward_event_e8s_equivalent as f64;
```

**File:** rs/nns/governance/src/governance.rs (L6722-6732)
```rust
            for (neuron_id, used_voting_rights) in voters_to_used_voting_right {
                if self.neuron_store.contains(neuron_id) {
                    let reward = (used_voting_rights * total_available_e8s_equivalent_float
                        / total_voting_rights) as u64;

                    reward_distribution.add_reward(neuron_id, reward);

                    // NOTE: This is the only reason we are checking the existence of neurons
                    // at this stage. Otherwise, we could defer until we distribute them in the
                    // schedule task.
                    actually_distributed_e8s_equivalent += reward;
```

**File:** rs/nns/governance/src/governance.rs (L6747-6757)
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

**File:** rs/nns/governance/src/reward/calculation.rs (L120-147)
```rust
    pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
        if self.rewards_rolled_over() {
            self.total_available_e8s_equivalent
        } else {
            0
        }
    }

    /// Calculates the rounds_since_last_distribution in this event that should
    /// be "rolled over" into the next `RewardEvent`.
    ///
    /// Behavior:
    /// - If rewards were distributed for this event, then no rounds should be
    ///   rolled over, so this function returns 0.
    /// - Otherwise, this function returns
    ///   `rounds_since_last_distribution`.
    pub(crate) fn rounds_since_last_distribution_to_be_rolled_over(&self) -> u64 {
        if self.rewards_rolled_over() {
            self.rounds_since_last_distribution.unwrap_or(0)
        } else {
            0
        }
    }

    /// Whether this is a "rollover event", where no rewards were distributed.
    pub(crate) fn rewards_rolled_over(&self) -> bool {
        self.settled_proposals.is_empty()
    }
```

**File:** rs/sns/governance/src/governance.rs (L5940-5942)
```rust
        // Because of rounding (and other shenanigans), it is possible that some
        // portion of this amount ends up not being actually distributed.
        let mut distributed_e8s_equivalent = 0_u64;
```

**File:** rs/sns/governance/src/governance.rs (L5973-5978)
```rust
                // Dividing before multiplying maximizes our chances of success.
                let neuron_reward_e8s =
                    rewards_purse_e8s * (neuron_reward_shares / total_reward_shares);

                // Round down, and convert to u64.
                let neuron_reward_e8s = u64::try_from(neuron_reward_e8s).unwrap_or_else(|err| {
```

**File:** rs/nns/governance/tests/governance.rs (L3893-3909)
```rust
/// In this test, there are 4 neurons, which are not always active: they
/// participate actively (as proposer or voter) on 3/4 of the proposals. Since
/// they are all behaving similarly, they all get an identical maturity.
/// Total maturity is 100 and we have 4 neurons and 4 proposals. Hence every vote is worth
/// 100/(4*4)=6.25
/// Thus a neuron that votes 3 times, receives 3*6.25 = 18.75 truncated to 18.
#[test]
fn test_neuron_sometimes_active_sometimes_passive_which_proposal_does_not_matter() {
    assert_eq!(
        compute_maturities(
            vec![1, 1, 1, 1],
            vec!["-Pyn", "P-yn", "Py-n", "Pyn-"],
            USUAL_REWARD_POT_E8S
        ),
        vec![18, 18, 18, 18]
    );
}
```
