### Title
SNS Governance Voting Rewards Permanently Lost When `total_reward_shares` Is Zero During Proposal Settlement - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

In the SNS governance canister, when a reward round has proposals to settle but all neurons have zero voting power (`total_reward_shares == 0`), the entire rewards purse — including any rolled-over maturity from previous rounds — is permanently and silently discarded. The rollover guard incorrectly uses `settled_proposals.is_empty()` as a proxy for "were rewards distributed?", causing the purse to be dropped rather than carried forward whenever proposals are settled in a zero-voting-power round.

---

### Finding Description

`distribute_rewards` in `rs/sns/governance/src/governance.rs` builds a `rewards_purse_e8s` that starts from any previously rolled-over maturity and adds the current round's supply-proportional reward:

```
result = e8s_equivalent_to_be_rolled_over()   // prior rollover
       + supply * reward_rate * round_duration  // new rewards
``` [1](#0-0) 

When `total_reward_shares == 0` (all neurons have zero net stake, i.e. `neuron_fees_e8s >= cached_neuron_stake_e8s`), the distribution loop is skipped entirely and `distributed_e8s_equivalent` stays at `0`: [2](#0-1) 

The function then writes the final `RewardEvent` with `settled_proposals` set to the non-empty list of proposals that were just settled, and `total_available_e8s_equivalent` set to the non-zero purse: [3](#0-2) 

In the **next** reward round, the rollover helper is consulted:

```rust
pub(crate) fn rewards_rolled_over(&self) -> bool {
    self.settled_proposals.is_empty()   // ← only checks proposals, not distribution
}

pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
    if self.rewards_rolled_over() {
        self.total_available_e8s_equivalent.unwrap_or_default()
    } else {
        0   // ← returned when settled_proposals is non-empty
    }
}
``` [4](#0-3) 

Because `settled_proposals` is non-empty, `rewards_rolled_over()` returns `false`, `e8s_equivalent_to_be_rolled_over()` returns `0`, and the entire purse — including any maturity accumulated over many prior rollover rounds — is permanently lost. There is no recovery path.

The analogous NNS rollover guard has the same structural flaw: [5](#0-4) 

---

### Impact Explanation

Any SNS governance token maturity that was accumulated in the `rewards_purse_e8s` (whether from the current round's supply-proportional reward or from prior rollover rounds) is permanently destroyed the moment a proposal is settled in a round where every neuron has zero voting power. The maturity is never credited to any neuron and is never carried forward. SNS token holders who expected to receive voting rewards for that period receive nothing, and the shortfall cannot be recovered.

---

### Likelihood Explanation

The condition `total_reward_shares == 0` is reachable without any privileged access:

1. **Depleted neurons**: Any neuron whose `neuron_fees_e8s >= cached_neuron_stake_e8s` has zero voting power. Fees accumulate automatically on rejected proposals. The existing test `zero_total_reward_shares` in `rs/sns/integration_tests/src/neuron.rs` explicitly constructs this state.
2. **Proposal settlement**: Any user holding a neuron (even a depleted one) can submit a proposal. Once the voting period expires the proposal enters `ReadyToSettle` and is picked up by the next periodic reward task.
3. **Automatic trigger**: `run_periodic_tasks` calls `distribute_rewards` automatically; no privileged call is needed. [6](#0-5) [7](#0-6) 

The scenario is most likely in early-stage SNS deployments where all founding neurons have been fee-depleted, or in any SNS where a governance attack deliberately drains neuron stakes via repeated rejected proposals before a reward round closes.

---

### Recommendation

The `rewards_rolled_over()` predicate must reflect whether rewards were **actually distributed**, not merely whether proposals were settled. Two equivalent fixes:

1. **Track distribution explicitly**: Add a boolean `rewards_were_distributed` to `RewardEvent` and set it only when `distributed_e8s_equivalent > 0`. Use this flag in `rewards_rolled_over()`.

2. **Roll over on zero distribution**: In `distribute_rewards`, when `total_reward_shares == 0` and `considered_proposals` is non-empty, explicitly carry the purse forward by writing `settled_proposals: vec![]` (or a dedicated rollover marker) so the existing rollover path is taken.

The NNS governance `calculate_voting_rewards` / `e8s_equivalent_to_be_rolled_over` pair has the same structural issue and should be reviewed in parallel. [8](#0-7) 

---

### Proof of Concept

```
State setup (mirrors rs/sns/integration_tests/src/neuron.rs:1226-1238):
  neuron_1.cached_neuron_stake_e8s = 1_000_000_000
  neuron_1.neuron_fees_e8s         = 1_000_000_000
  → voting_power(neuron_1) == 0

1. Submit proposal P1; neuron_1 votes Yes (voting_power = 0).
2. Wait for P1 to reach ReadyToSettle.
3. Assume prior rollover rounds have accumulated
   rewards_purse_e8s = R > 0 in latest_reward_event.total_available_e8s_equivalent.
4. run_periodic_tasks() fires → distribute_rewards(supply) is called.
5. rewards_purse_e8s = R + supply * rate * duration  (> 0)
6. total_reward_shares = 0  → distribution loop skipped, distributed_e8s_equivalent = 0.
7. RewardEvent written:
     settled_proposals            = [P1]   ← non-empty
     distributed_e8s_equivalent   = 0
     total_available_e8s_equivalent = R'   ← non-zero
8. Next round: e8s_equivalent_to_be_rolled_over()
     rewards_rolled_over() → settled_proposals.is_empty() → false
     → returns 0
9. rewards_purse_e8s for next round = 0 + supply * rate * duration
   (R' is gone; no neuron ever receives it)
``` [9](#0-8) [10](#0-9) [4](#0-3)

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

**File:** rs/sns/governance/src/governance.rs (L5946-5953)
```rust
        if total_reward_shares == dec!(0) {
            log!(
                ERROR,
                "Warning: total_reward_shares is 0. Therefore, we skip increasing \
                 neuron maturity. neuron_id_to_reward_shares: {:#?}",
                neuron_id_to_reward_shares,
            );
        } else {
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

**File:** rs/nns/governance/src/reward/calculation.rs (L120-126)
```rust
    pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
        if self.rewards_rolled_over() {
            self.total_available_e8s_equivalent
        } else {
            0
        }
    }
```

**File:** rs/nns/governance/src/reward/calculation.rs (L144-147)
```rust
    /// Whether this is a "rollover event", where no rewards were distributed.
    pub(crate) fn rewards_rolled_over(&self) -> bool {
        self.settled_proposals.is_empty()
    }
```

**File:** rs/sns/integration_tests/src/neuron.rs (L1168-1238)
```rust
async fn zero_total_reward_shares() {
    // Step 1: Prepare the world.

    struct EmptyLedger {}
    #[async_trait]
    impl ICRC1Ledger for EmptyLedger {
        async fn transfer_funds(
            &self,
            _amount_e8s: u64,
            _fee_e8s: u64,
            _from_subaccount: Option<Subaccount>,
            _to: Account,
            _memo: u64,
        ) -> Result<u64, NervousSystemError> {
            unimplemented!();
        }

        async fn total_supply(&self) -> Result<Tokens, NervousSystemError> {
            Ok(Tokens::from_e8s(0))
        }

        async fn account_balance(&self, _account: Account) -> Result<Tokens, NervousSystemError> {
            Ok(Tokens::from_e8s(0))
        }

        fn canister_id(&self) -> CanisterId {
            CanisterId::from_u64(1)
        }

        async fn icrc2_approve(
            &self,
            _spender: Account,
            _amount: u64,
            _expires_at: Option<u64>,
            _fee: u64,
            _from_subaccount: Option<Subaccount>,
            _expected_allowance: Option<u64>,
        ) -> Result<Nat, NervousSystemError> {
            Err(NervousSystemError {
                error_message: "Not Implemented".to_string(),
            })
        }

        async fn icrc3_get_blocks(
            &self,
            _args: Vec<GetBlocksRequest>,
        ) -> Result<GetBlocksResult, NervousSystemError> {
            Err(NervousSystemError {
                error_message: "Not Implemented".to_string(),
            })
        }
    }

    let environment = NativeEnvironment::default();
    let now = environment.now();

    let genesis_timestamp_seconds = 1;

    // Step 1.1: Craft a neuron with a "net" stake (i.e. cached stake - fees) of 0.
    let neuron_id = NeuronId { id: vec![1, 2, 3] };
    // A number whose only significance is that it is not Protocol Buffers default (i.e. 0.0).
    let maturity_e8s_equivalent = 3;
    let depleted_neuron = Neuron {
        id: Some(neuron_id.clone()),
        cached_neuron_stake_e8s: 1_000_000_000,
        neuron_fees_e8s: 1_000_000_000,
        maturity_e8s_equivalent,
        ..Default::default()
    };
    let voting_power = depleted_neuron.voting_power(now, 60, 60, 100, 25);
    assert_eq!(voting_power, 0);
```
