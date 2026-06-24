### Title
SNS Governance Voting Rewards Purse Permanently Lost When `total_reward_shares` Is Zero With Non-Empty Settled Proposals - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

In SNS Governance's `distribute_rewards`, when proposals exist in `ReadyToSettle` state but no neuron voted on any of them (`total_reward_shares == dec!(0)`), the computed `rewards_purse_e8s` is consumed — the proposals are marked as settled and `latest_reward_event` is updated — but zero maturity is distributed and the purse is **not rolled over** to the next round. The rewards are permanently destroyed. This is a direct IC analog of the `BabelVault`/`EmissionSchedule` bug: a "supply" pool decreases without any corresponding allocation to receivers.

---

### Finding Description

`distribute_rewards` in `rs/sns/governance/src/governance.rs` computes `rewards_purse_e8s` (lines 5854–5875) by accumulating the rolled-over purse from the previous `RewardEvent` plus the current round's interest on the total token supply:

```rust
let rewards_purse_e8s = {
    let mut result = Decimal::from(
        self.latest_reward_event()
            .e8s_equivalent_to_be_rolled_over(),
    );
    // ... adds supply * reward_rate * round_duration for each new round
    result
};
```

It then tallies exercised voting power across all `ReadyToSettle` proposals:

```rust
let total_reward_shares: Decimal = neuron_id_to_reward_shares.values().sum();
if total_reward_shares == dec!(0) {
    log!(ERROR, "Warning: total_reward_shares is 0. Therefore, we skip increasing neuron maturity...");
} else {
    // distribute rewards to neurons
}
```

Regardless of whether maturity was distributed, the function always concludes by writing a new `latest_reward_event` with `settled_proposals = considered_proposals` (non-empty) and `distributed_e8s_equivalent = 0`:

```rust
self.proto.latest_reward_event = Some(RewardEvent {
    settled_proposals: considered_proposals,   // non-empty
    distributed_e8s_equivalent,               // 0
    total_available_e8s_equivalent,           // Some(rewards_purse_e8s as u64)
    ...
})
```

The rollover mechanism is gated on `settled_proposals.is_empty()`. In `rs/sns/governance/src/types.rs` (and documented in the proto at `rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto` lines 1405–1415):

> *"The `e8s_equivalent_to_be_rolled_over` method returns this when there are no proposals (per the `settled_proposals` field)."*

Because `settled_proposals` is **non-empty**, `e8s_equivalent_to_be_rolled_over()` returns `0` in the next round. The entire `rewards_purse_e8s` — including any previously rolled-over amounts — is permanently lost.

The identical structural flaw exists in NNS Governance's `calculate_voting_rewards` (`rs/nns/governance/src/governance.rs` lines 6712–6719): when `total_voting_rights < 0.001` with non-empty `considered_proposals`, `reward_distribution = None`, `distributed_e8s_equivalent = 0`, but `settled_proposals` is non-empty, so `rewards_rolled_over()` returns `false` and `e8s_equivalent_to_be_rolled_over()` returns `0`.

---

### Impact Explanation

SNS token holders who staked neurons lose their voting rewards for the affected round. The `rewards_purse_e8s` — proportional to `total_token_supply × reward_rate × round_duration` — is permanently destroyed. Any previously accumulated rolled-over purse from prior empty rounds is also lost in the same event. This is a **governance ledger conservation bug**: the accounting records a consumed reward pool with zero distribution and no carry-forward.

---

### Likelihood Explanation

For SNS Governance: an SNS can have proposals enter `ReadyToSettle` state without any neuron casting a vote (e.g., all neurons abstain, follow a neuron that does not vote, or the SNS is newly launched with low participation). This is a realistic operational scenario, especially for newly deployed SNS instances. The `run_periodic_tasks` timer fires automatically, so no privileged action is required to trigger the path.

For NNS Governance: the `total_voting_rights < 0.001` condition requires all `ReadyToSettle` proposals to have `total_potential_voting_power ≈ 0`, which is extremely unlikely in the live NNS but theoretically possible (the code itself acknowledges: *"Not sure if that is theoretically possible, but even if it isn't, it might occur due to some bug"*).

---

### Recommendation

In `distribute_rewards` (SNS) and `calculate_voting_rewards` (NNS), when `total_reward_shares == 0` (or `total_voting_rights < 0.001`) with non-empty `considered_proposals`, the rewards purse should be preserved rather than silently consumed. Two options:

1. **Do not settle the proposals** in this case — keep them in `ReadyToSettle` so the next round can attempt distribution.
2. **Modify the rollover predicate** so that `e8s_equivalent_to_be_rolled_over()` also returns `total_available_e8s_equivalent` when `distributed_e8s_equivalent == 0` regardless of whether `settled_proposals` is empty.

Option 1 is safer as it avoids changing the rollover accounting semantics.

---

### Proof of Concept

**SNS path (most realistic):**

1. Deploy an SNS with `voting_rewards_parameters` set (non-zero reward rate).
2. Create a proposal; let it reach `ReadyToSettle` state (voting period expires) with **no neuron casting a vote**.
3. Advance time past `round_duration_seconds`.
4. `run_periodic_tasks` fires → `should_distribute_rewards()` returns `true` → `distribute_rewards(supply)` is called.
5. `considered_proposals` is non-empty (the expired proposal).
6. `neuron_id_to_reward_shares` is empty (no votes cast) → `total_reward_shares == dec!(0)`.
7. The `if total_reward_shares == dec!(0)` branch logs a warning and skips maturity distribution.
8. `latest_reward_event` is written with `settled_proposals = [proposal_id]`, `distributed_e8s_equivalent = 0`, `total_available_e8s_equivalent = Some(purse)`.
9. In the next round, `e8s_equivalent_to_be_rolled_over()` returns `0` because `settled_proposals` is non-empty.
10. The entire `rewards_purse_e8s` from step 4 is permanently lost.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** rs/sns/governance/src/governance.rs (L6083-6093)
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
    }
```

**File:** rs/nns/governance/src/governance.rs (L6712-6719)
```rust
        let reward_distribution = if total_voting_rights < 0.001 {
            println!(
                "{}WARNING: total_voting_rights == {}, even though considered_proposals \
                 is nonempty (see earlier log). Therefore, we skip incrementing maturity \
                 to avoid dividing by zero (or super small number).",
                LOG_PREFIX, total_voting_rights,
            );
            None
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

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1405-1415)
```text
  // The total amount of rewards that was available during the reward event.
  //
  // The e8s_equivalent_to_be_rolled_over method returns this when
  // there are no proposals (per the settled_proposals field).
  //
  // This is mostly copied from NNS.
  //
  // Warning: There is a field with the same name in NNS, but different tags are
  // used. Also, this uses the `optional` keyword (whereas, the NNS analog does
  // not).
  optional uint64 total_available_e8s_equivalent = 8;
```
