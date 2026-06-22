### Title
Voting Rewards Purse Permanently Lost When `total_reward_shares` Is Zero With Non-Empty Settled Proposals — (`rs/sns/governance/src/governance.rs`)

---

### Summary

In SNS governance's `distribute_rewards`, when `total_reward_shares == dec!(0)` (e.g., all voting neurons have zero net stake), the entire `rewards_purse_e8s` is silently discarded. The proposals are still marked as settled in the same call, which prevents the rewards from rolling over to the next round. The rewards are permanently unrecoverable. An identical structural bug exists in NNS governance's `calculate_voting_rewards` when `total_voting_rights < 0.001`.

---

### Finding Description

In `rs/sns/governance/src/governance.rs`, the `distribute_rewards` function computes a `rewards_purse_e8s` from the token supply and reward rate, then attempts to distribute it proportionally to neurons based on their voting power shares:

```rust
// rs/sns/governance/src/governance.rs:5946-5952
if total_reward_shares == dec!(0) {
    log!(
        ERROR,
        "Warning: total_reward_shares is 0. Therefore, we skip increasing \
         neuron maturity. neuron_id_to_reward_shares: {:#?}",
        neuron_id_to_reward_shares,
    );
} else {
    // distribute rewards_purse_e8s to neurons ...
}
```

When `total_reward_shares == 0`, the guard correctly avoids a division-by-zero. However, execution continues and the proposals are unconditionally settled:

```rust
// rs/sns/governance/src/governance.rs:6083-6092
self.proto.latest_reward_event = Some(RewardEvent {
    round: new_reward_event_round,
    actual_timestamp_seconds: now,
    settled_proposals: considered_proposals,   // non-empty
    distributed_e8s_equivalent,               // = 0
    end_timestamp_seconds: Some(reward_event_end_timestamp_seconds),
    rounds_since_last_distribution: Some(new_rounds_count),
    total_available_e8s_equivalent,           // = Some(rewards_purse_e8s)
})
```

The rollover logic in `rs/sns/governance/src/types.rs` determines whether the purse carries forward:

```rust
// rs/sns/governance/src/types.rs:2065-2067
pub(crate) fn rewards_rolled_over(&self) -> bool {
    self.settled_proposals.is_empty()
}
```

Because `settled_proposals` is non-empty, `rewards_rolled_over()` returns `false`, so `e8s_equivalent_to_be_rolled_over()` returns `0`. The `rewards_purse_e8s` — which may include accumulated rollover from many previous rounds — is permanently lost with no recovery path.

The same structural bug exists in NNS governance:

```rust
// rs/nns/governance/src/governance.rs:6712-6719
let reward_distribution = if total_voting_rights < 0.001 {
    println!("{}WARNING: total_voting_rights == {} ... skip incrementing maturity ...",
        LOG_PREFIX, total_voting_rights);
    None
} else { ... };
```

The NNS `RewardEvent` is also written with non-empty `settled_proposals` and `distributed_e8s_equivalent = 0`, and `rewards_rolled_over()` returns `false` for the same reason.

The scenario is explicitly demonstrated by the existing test `zero_total_reward_shares` in `rs/sns/integration_tests/src/neuron.rs`, which constructs a neuron with `cached_neuron_stake_e8s == neuron_fees_e8s` (net stake = 0, voting power = 0), votes on a proposal, and confirms `distributed_e8s_equivalent == 0` — with no assertion that the purse was rolled over.

---

### Impact Explanation

The entire `rewards_purse_e8s` for the affected round — including any accumulated rollover from prior rounds where no proposals were settled — is permanently destroyed. Neurons that should have received maturity receive nothing, and the maturity can never be recovered. In an SNS with a large rolled-over purse, this could represent a significant fraction of the total token supply's annual reward allocation.

---

### Likelihood Explanation

**SNS**: Low but non-zero and demonstrably reachable. A neuron whose `neuron_fees_e8s >= cached_neuron_stake_e8s` has voting power 0. If such a neuron is the only voter on a `ReadyToSettle` proposal (all other neurons abstain or follow no one), `total_reward_shares == 0` and the purse is lost. In small or early-stage SNS deployments with few active neurons, this edge case is realistic. An adversarial participant can deliberately accumulate fees (by submitting proposals that get rejected) to reach net-zero stake, then vote alone on a proposal.

**NNS**: Very low. The minimum nonzero `total_voting_rights` is 0.01 (from the 0.01x proposal weight), so the `< 0.001` threshold is extremely hard to reach in practice on mainnet.

---

### Recommendation

When `total_reward_shares == 0` (SNS) or `total_voting_rights < 0.001` (NNS) but `considered_proposals` is non-empty, the function should either:

1. **Not settle the proposals** — leave them in `ReadyToSettle` so the purse rolls over naturally, or
2. **Explicitly roll over the purse** — write the `RewardEvent` with `settled_proposals: vec![]` (or an equivalent flag) so that `rewards_rolled_over()` returns `true` and `e8s_equivalent_to_be_rolled_over()` returns the full purse.

Option 2 is simpler. For SNS:

```rust
if total_reward_shares == dec!(0) {
    // Roll over: write event with empty settled_proposals so purse carries forward.
    self.proto.latest_reward_event = Some(RewardEvent {
        settled_proposals: vec![],  // triggers rollover
        distributed_e8s_equivalent: 0,
        total_available_e8s_equivalent,
        ...
    });
    return;
}
```

---

### Proof of Concept

The existing test at `rs/sns/integration_tests/src/neuron.rs:1168` (`zero_total_reward_shares`) already demonstrates the root cause. To confirm the purse is lost rather than rolled over, extend the test:

```rust
// After governance.run_periodic_tasks().await:
let reward_event = governance.proto.latest_reward_event.as_ref().unwrap();
// Proposals were settled, so rewards_rolled_over() == false
assert!(!reward_event.settled_proposals.is_empty());
// Purse was non-zero (supply > 0, reward rate > 0)
assert!(reward_event.total_available_e8s_equivalent.unwrap_or(0) > 0);
// Nothing was distributed
assert_eq!(reward_event.distributed_e8s_equivalent, 0);
// Next round: rolled-over amount is 0 — purse is gone
assert_eq!(reward_event.e8s_equivalent_to_be_rolled_over(), 0);
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

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

**File:** rs/sns/governance/src/types.rs (L2054-2060)
```rust
    pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
        if self.rewards_rolled_over() {
            self.total_available_e8s_equivalent.unwrap_or_default()
        } else {
            0
        }
    }
```

**File:** rs/sns/governance/src/types.rs (L2064-2067)
```rust
    /// Whether this is a "rollover event", where no rewards were distributed.
    pub(crate) fn rewards_rolled_over(&self) -> bool {
        self.settled_proposals.is_empty()
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

**File:** rs/sns/integration_tests/src/neuron.rs (L1168-1359)
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

    // Step 1.2: Craft a ProposalData that is ReadyToSettle.
    let proposal_id = 99;
    let do_nothing_proposal = Proposal {
        action: Some(Action::Motion(Motion {
            motion_text: "For great justice.".to_string(),
        })),
        ..Default::default()
    };
    let ready_to_settle_proposal_data = ProposalData {
        id: Some(ProposalId { id: proposal_id }),
        proposal: Some(do_nothing_proposal),
        ballots: btreemap! {
            depleted_neuron.id.as_ref().unwrap().to_string() => Ballot {
                vote: Vote::Yes as i32,
                voting_power,
                cast_timestamp_seconds: now,
            },
        },
        wait_for_quiet_state: Some(WaitForQuietState::default()),
        is_eligible_for_rewards: true,
        ..Default::default()
    };
    assert_eq!(
        ready_to_settle_proposal_data.reward_status(now),
        ProposalRewardStatus::ReadyToSettle,
    );

    // Step 1.3: Craft a governance.
    let root_canister_id = [1; 29];
    let ledger_canister_id = [2; 29];
    let swap_canister_id = [3; 29];
    let proto = GovernanceProto {
        // These won't be used, so we use garbage values.
        root_canister_id: Some(PrincipalId::new(29, root_canister_id)),
        ledger_canister_id: Some(PrincipalId::new(29, ledger_canister_id)),
        swap_canister_id: Some(PrincipalId::new(29, swap_canister_id)),
        parameters: Some(NervousSystemParameters {
            voting_rewards_parameters: Some(VOTING_REWARDS_PARAMETERS),
            ..NervousSystemParameters::with_default_values()
        }),
        mode: governance::Mode::Normal as i32,

        genesis_timestamp_seconds,

        proposals: btreemap! {
            ready_to_settle_proposal_data.id.unwrap().id => ready_to_settle_proposal_data,
        },
        neurons: btreemap! {
            depleted_neuron.id.as_ref().unwrap().to_string() => depleted_neuron,
        },

        // Last reward event was a "long time ago".
        // This should cause rewards to be distributed.
        latest_reward_event: Some(RewardEvent {
            round: 1,
            actual_timestamp_seconds: 1,
            settled_proposals: vec![],
            distributed_e8s_equivalent: 0,
            end_timestamp_seconds: Some(1),
            rounds_since_last_distribution: Some(1),
            total_available_e8s_equivalent: None,
        }),
        sns_metadata: Some(SnsMetadata {
            logo: Some("data:image/png;base64,aGVsbG8gZnJvbSBkZmluaXR5IQ==".to_string()),
            url: Some("https://internetcomputer.org/".to_string()),
            name: Some("ServiceNervousSystemTest".to_string()),
            description: Some("A project testing the SNS".to_string()),
        }),
        metrics: Some(GovernanceCachedMetrics {
            // This disables refreshing the cached metrics in periodic tasks.
            timestamp_seconds: u64::MAX,
            ..Default::default()
        }),
        ..Default::default()
    };
    let mut governance = Governance::new(
        proto.try_into().unwrap(),
        Box::new(environment),
        Box::new(EmptyLedger {}),
        Box::new(EmptyLedger {}),
        Box::new(FakeCmc::new()),
    );
    // Prevent gc.
    governance.latest_gc_timestamp_seconds = now;

    // Step 2: Run code under test.
    governance.run_periodic_tasks().await;

    // Step 3: Inspect results. The main thing is to make sure that we did not
    // divide by zero. If that happened, it would show up in a couple places:
    // neuron maturity, and latest_reward_event.

    // Step 3.1: Inspect the neuron.
    let neuron = governance
        .proto
        .neurons
        .get(&neuron_id.to_string())
        .unwrap();
    // We expect no change to the neuron's maturity.
    assert_eq!(
        neuron.maturity_e8s_equivalent, maturity_e8s_equivalent,
        "neuron: {neuron:#?}",
    );

    // Step 3.2: Inspect the latest_reward_event.
    let reward_event = governance.proto.latest_reward_event.as_ref().unwrap();
    assert_eq!(
        reward_event
            .settled_proposals
            .iter()
            .map(|p| p.id)
            .collect::<Vec<_>>(),
        vec![proposal_id],
        "{reward_event:#?}",
    );
    assert_eq!(
        reward_event.distributed_e8s_equivalent, 0,
        "{reward_event:#?}",
    );
}
```
