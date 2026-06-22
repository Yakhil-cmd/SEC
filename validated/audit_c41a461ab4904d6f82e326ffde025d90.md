### Title
Voting Rewards Permanently Lost When No Neurons Vote on Settled Proposals in SNS Governance - (`rs/sns/governance/src/governance.rs`)

### Summary

In the SNS governance canister, when a reward round contains settled proposals but `total_reward_shares == 0` (no eligible votes were cast), the entire rewards purse for that round is silently discarded — neither distributed to neurons nor rolled over to the next round. This is the direct IC analog of the Wenwin M-07 bug: rewards are lost when there are no participants to receive them, because the rollover condition is keyed solely on whether proposals were settled, not on whether rewards were actually distributed.

### Finding Description

In `rs/sns/governance/src/governance.rs`, the `distribute_rewards` function computes a `rewards_purse_e8s` from the token supply and the reward rate. It then tallies `total_reward_shares` by summing the voting power of all neurons that cast an eligible vote (`Vote::Yes` or `Vote::No`) on each settled proposal. [1](#0-0) 

When `total_reward_shares == dec!(0)`, the code logs a warning and skips distributing maturity to any neuron, leaving `distributed_e8s_equivalent = 0`. The function then writes the `RewardEvent` with the non-empty `considered_proposals` list and `distributed_e8s_equivalent = 0`: [2](#0-1) 

The rollover mechanism in `rs/sns/governance/src/types.rs` determines whether the rewards purse carries forward by checking `rewards_rolled_over()`, which returns `true` only when `settled_proposals.is_empty()`: [3](#0-2) 

Because `settled_proposals` is non-empty (proposals were settled, just not voted on), `rewards_rolled_over()` returns `false`, and `e8s_equivalent_to_be_rolled_over()` returns `0`. In the next reward round, the purse calculation starts from zero: [4](#0-3) 

The rewards purse from the round where no one voted is permanently lost — it is not minted as maturity for any neuron, and it is not carried forward.

The NNS governance has the same structural flaw via its own `e8s_equivalent_to_be_rolled_over` / `rewards_rolled_over` pair: [5](#0-4) 

### Impact Explanation

Voting rewards (maturity e8s equivalent) that were legitimately accrued during a reward round are permanently destroyed when no eligible votes were cast on the settled proposals. The total maturity that should exist in the SNS system is lower than the protocol intends. Stakers who participate in future rounds receive a smaller share of the cumulative reward pool than they are entitled to. The loss is irreversible: once the `RewardEvent` is written with non-empty `settled_proposals`, the purse from that round can never be recovered.

### Likelihood Explanation

This condition — settled proposals with zero eligible votes — is reachable in several realistic SNS scenarios:

1. **Early SNS lifecycle**: A proposal is submitted and reaches `ReadyToSettle` before any neuron has configured followees or cast a direct vote. All ballots remain `Vote::Unspecified`, which is filtered out by `eligible_for_rewards()`.
2. **All voting neurons dissolved before reward event**: Neurons that voted are dissolved and their records deleted between the proposal settling and the periodic `run_periodic_tasks` call that fires `distribute_rewards`.
3. **Abstention-only round**: All neurons abstain (cast `Vote::Unspecified`) on every proposal in a round.

The `run_periodic_tasks` call that triggers `distribute_rewards` is an unprivileged periodic canister heartbeat — no special role is required to reach this code path. [6](#0-5) 

### Recommendation

The rollover condition should be based on whether rewards were actually distributed, not solely on whether proposals were settled. Specifically, `rewards_rolled_over()` should return `true` when `distributed_e8s_equivalent == 0` regardless of whether `settled_proposals` is empty. Alternatively, when `total_reward_shares == 0` and `considered_proposals` is non-empty, the function should either:

- Roll the full `rewards_purse_e8s` into `total_available_e8s_equivalent` and treat the event as a rollover, or
- Set `settled_proposals` to empty for reward-accounting purposes while still marking the proposals as settled in proposal state.

The fix in `e8s_equivalent_to_be_rolled_over` would be:

```rust
pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
    if self.distributed_e8s_equivalent == 0 {
        self.total_available_e8s_equivalent.unwrap_or_default()
    } else {
        0
    }
}
```

### Proof of Concept

**Scenario (SNS):**

1. Deploy an SNS with `VotingRewardsParameters` configured.
2. Submit a proposal. All neuron ballots default to `Vote::Unspecified`; no neuron votes directly or via followees.
3. Wait for the proposal's voting period to expire → proposal enters `ReadyToSettle`.
4. Wait for `round_duration_seconds` to elapse → `should_distribute_rewards()` returns `true`.
5. `run_periodic_tasks` calls `distribute_rewards(supply)`.
6. `considered_proposals` is non-empty (the proposal is ready to settle).
7. `total_reward_shares == dec!(0)` because no ballot has an eligible vote.
8. `distributed_e8s_equivalent` stays `0`.
9. `RewardEvent` is written: `settled_proposals = [proposal_id]`, `distributed_e8s_equivalent = 0`, `total_available_e8s_equivalent = X` (non-zero).
10. Next reward round: `e8s_equivalent_to_be_rolled_over()` returns `0` because `settled_proposals` is non-empty → the purse `X` is gone.

Observable: `total_available_e8s_equivalent` in the second `RewardEvent` equals only the new round's accrual, not `X + new_accrual`. [7](#0-6) [3](#0-2)

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

**File:** rs/sns/governance/src/governance.rs (L5854-5858)
```rust
        let rewards_purse_e8s = {
            let mut result = Decimal::from(
                self.latest_reward_event()
                    .e8s_equivalent_to_be_rolled_over(),
            );
```

**File:** rs/sns/governance/src/governance.rs (L5934-5952)
```rust
        let total_reward_shares: Decimal = neuron_id_to_reward_shares.values().sum();
        debug_assert!(
            total_reward_shares >= dec!(0),
            "total_reward_shares: {total_reward_shares} neuron_id_to_reward_shares: {neuron_id_to_reward_shares:#?}",
        );

        // Because of rounding (and other shenanigans), it is possible that some
        // portion of this amount ends up not being actually distributed.
        let mut distributed_e8s_equivalent = 0_u64;
        // Now that we know the size of the pie (rewards_purse_e8s), and how
        // much of it each neuron is supposed to get (*_reward_shares), we now
        // proceed to actually handing out those rewards.
        if total_reward_shares == dec!(0) {
            log!(
                ERROR,
                "Warning: total_reward_shares is 0. Therefore, we skip increasing \
                 neuron maturity. neuron_id_to_reward_shares: {:#?}",
                neuron_id_to_reward_shares,
            );
```

**File:** rs/sns/governance/src/governance.rs (L6083-6092)
```rust
        // Conclude this round of rewards.
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

**File:** rs/sns/governance/src/types.rs (L2054-2067)
```rust
    pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
        if self.rewards_rolled_over() {
            self.total_available_e8s_equivalent.unwrap_or_default()
        } else {
            0
        }
    }

    // Not copied from NNS: fn rounds_since_last_distribution_to_be_rolled_over

    /// Whether this is a "rollover event", where no rewards were distributed.
    pub(crate) fn rewards_rolled_over(&self) -> bool {
        self.settled_proposals.is_empty()
    }
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
