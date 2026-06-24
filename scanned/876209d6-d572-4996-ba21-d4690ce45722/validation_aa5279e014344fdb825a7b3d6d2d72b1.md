### Title
SNS Voting Rewards Permanently Lost When Proposals Settle With Zero Voter Participation - (`File: rs/sns/governance/src/governance.rs`, `rs/sns/governance/src/types.rs`)

### Summary

The SNS governance `distribute_rewards` function permanently discards the entire accumulated rewards purse when proposals are settled in a round where no neuron cast an eligible vote (`total_reward_shares == 0`). The rollover mechanism only triggers when `settled_proposals` is empty, so a round with settled-but-unvoted proposals silently destroys the purse rather than carrying it forward.

### Finding Description

In `rs/sns/governance/src/governance.rs`, `distribute_rewards` computes `rewards_purse_e8s` — which includes any rolled-over balance from prior rounds plus new rewards for the current rounds — and then attempts to apportion it among neurons proportional to their voting shares: [1](#0-0) 

When `total_reward_shares == dec!(0)` (no neuron voted on any settled proposal), the function logs a warning and skips maturity distribution, leaving `distributed_e8s_equivalent = 0`: [2](#0-1) 

Critically, the function does **not** return early. It continues to settle all `considered_proposals` (clearing their ballots and stamping `reward_event_end_timestamp_seconds`), then writes a new `RewardEvent` with `settled_proposals` populated and `distributed_e8s_equivalent = 0`.

The rollover logic lives in `rs/sns/governance/src/types.rs`: [3](#0-2) 

`rewards_rolled_over()` returns `true` **only** when `settled_proposals.is_empty()`. Because proposals were just settled, `settled_proposals` is non-empty, so `e8s_equivalent_to_be_rolled_over()` returns `0`. On the next call to `distribute_rewards`, the purse calculation starts from zero: [4](#0-3) 

The entire `rewards_purse_e8s` — which may include multiple rounds of rolled-over rewards — is permanently discarded. No minting occurs, no neuron receives maturity, and no recovery path exists.

The NNS governance has a structurally identical guard at `total_voting_rights < 0.001`: [5](#0-4) 

However, the NNS case is far less likely in practice given the large number of active neurons.

### Impact Explanation

Any SNS voting rewards accumulated during a round in which proposals settle but no neuron casts an eligible vote are permanently destroyed. The loss scales with `supply × reward_rate × round_duration`. For a newly launched SNS with a high initial reward rate (e.g., 10% annualized), even a single missed round can represent a material fraction of the expected annual inflation budget. The rewards are never minted and cannot be recovered.

### Likelihood Explanation

The condition is reachable without any privileged access:

1. **Newly launched SNS**: During the initial swap period or immediately after launch, neurons may exist but have not yet configured following relationships. A proposal created and decided (rejected by default timeout) before any neuron votes triggers the bug.
2. **Abstention by all neurons**: If all neurons with voting power abstain on every proposal in a round, `total_reward_shares == 0` and the purse is lost.
3. **Unprivileged trigger**: Any account holding enough SNS tokens to pay the proposal rejection fee can submit a proposal. If the SNS community fails to vote before the voting period expires, the rewards for that round are gone.

The SNS proto documentation explicitly acknowledges rollover only for the no-proposals case: [6](#0-5) 

This confirms the rollover mechanism was not designed to handle the zero-voter-but-settled-proposals case.

### Recommendation

Change `rewards_rolled_over()` to check whether rewards were actually distributed, not merely whether proposals were settled:

```rust
pub(crate) fn rewards_rolled_over(&self) -> bool {
    // Roll over if no proposals were settled OR if proposals were settled
    // but no rewards were distributed (e.g., zero voter participation).
    self.settled_proposals.is_empty()
        || (self.distributed_e8s_equivalent == 0
            && self.total_available_e8s_equivalent.unwrap_or(0) > 0)
}
```

Alternatively, `e8s_equivalent_to_be_rolled_over` could return `total_available_e8s_equivalent - distributed_e8s_equivalent` unconditionally, which is the correct undistributed balance regardless of whether proposals were settled.

### Proof of Concept

```
1. Launch SNS with VotingRewardsParameters: round_duration = 7 days, initial_rate = 10%.
2. Wait one full round (7 days) with no neuron votes cast on any proposal.
   - A proposal is submitted and rejected by timeout (ReadyToSettle).
3. run_periodic_tasks() calls distribute_rewards(supply).
   - rewards_purse_e8s = supply * 0.10 / 52 ≈ substantial amount.
   - considered_proposals = [proposal_1]  (non-empty)
   - total_reward_shares = 0  (no votes cast)
   - distributed_e8s_equivalent = 0
   - new RewardEvent: settled_proposals=[proposal_1], distributed=0, total_available=purse
4. Next round: distribute_rewards() called again.
   - e8s_equivalent_to_be_rolled_over() → rewards_rolled_over() → settled_proposals.is_empty() = false → returns 0
   - rewards_purse_e8s starts from 0 (no rollover)
   - The entire prior purse is permanently lost.
```

### Citations

**File:** rs/sns/governance/src/governance.rs (L5854-5875)
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
```

**File:** rs/sns/governance/src/governance.rs (L5946-5952)
```rust
        if total_reward_shares == dec!(0) {
            log!(
                ERROR,
                "Warning: total_reward_shares is 0. Therefore, we skip increasing \
                 neuron maturity. neuron_id_to_reward_shares: {:#?}",
                neuron_id_to_reward_shares,
            );
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

**File:** rs/nns/governance/src/governance.rs (L5712-5719)
```rust
    /// Add or remove followees for this neuron for a specified topic.
    ///
    /// If the list of followees is empty, remove the followees for
    /// this topic. If the list has at least one element, replace the
    /// current list of followees for the given topic with the
    /// provided list. Note that the list is replaced, not added to.
    fn follow(
        &mut self,
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1399-1403)
```text
  // 2. Rollover: We tried to distribute rewards, but there were no proposals
  //    settled to distribute rewards for.
  //
  // In both of these cases, the rewards purse rolls over into the next round.
  optional uint64 rounds_since_last_distribution = 6;
```
