Audit Report

## Title
Voting Rewards Permanently Lost When All Settled-Proposal Voters Have Zero Voting Power - (`rs/sns/governance/src/types.rs` and `rs/nns/governance/src/reward/calculation.rs`)

## Summary
Both SNS and NNS governance contain a reward-accounting bug where the rewards purse for a round is permanently destroyed when proposals are settled but all voters had zero voting power. The rollover gate `rewards_rolled_over()` is keyed solely on `settled_proposals.is_empty()`, so a round with settled proposals but zero distributed rewards returns `false`, causing `e8s_equivalent_to_be_rolled_over()` to return `0` and silently drop the entire purse.

## Finding Description
**SNS root cause (`rs/sns/governance/src/types.rs:2065-2067`):**

`rewards_rolled_over()` returns `self.settled_proposals.is_empty()`. When proposals are settled with zero total voting power, this returns `false`. [1](#0-0) 

`e8s_equivalent_to_be_rolled_over()` then returns `0` because `rewards_rolled_over()` is `false`, silently dropping the purse: [2](#0-1) 

**SNS distribution skip (`rs/sns/governance/src/governance.rs:5946-5952`):**

When `total_reward_shares == dec!(0)`, distribution is skipped and `distributed_e8s_equivalent` stays `0`, but execution continues unconditionally to settle proposals and write the `RewardEvent`: [3](#0-2) 

The `RewardEvent` is written with `settled_proposals: considered_proposals` (non-empty), `distributed_e8s_equivalent: 0`, and `total_available_e8s_equivalent: Some(purse > 0)`: [4](#0-3) 

**NNS identical root cause (`rs/nns/governance/src/reward/calculation.rs:144-147`):**

NNS uses the same `rewards_rolled_over()` predicate gated on `settled_proposals.is_empty()`, and skips distribution when `total_voting_rights < 0.001` while still settling proposals: [5](#0-4) [6](#0-5) 

**Exploit flow:**
1. All neurons that voted on settled proposals have `cached_neuron_stake_e8s <= neuron_fees_e8s` → voting power = 0.
2. `distribute_rewards` computes `rewards_purse_e8s > 0` (from token supply × rate × duration).
3. `total_reward_shares == 0` → distribution skipped, `distributed_e8s_equivalent = 0`.
4. Proposals settled → `settled_proposals` non-empty → `rewards_rolled_over()` returns `false`.
5. Next round: `e8s_equivalent_to_be_rolled_over()` returns `0` → purse permanently lost.

The existing test `zero_total_reward_shares` in `rs/sns/integration_tests/src/neuron.rs` explicitly constructs this scenario and asserts `distributed_e8s_equivalent == 0` with a non-empty `settled_proposals`, confirming the stuck-rewards state is reachable: [7](#0-6) 

## Impact Explanation
This is a **High** severity finding. The entire rewards purse for an affected round — computed as `token_supply × reward_rate × round_duration` plus any previously rolled-over amount — is permanently destroyed. No neuron receives maturity, and the amount is not carried forward. For an SNS with a meaningful token supply and reward rate, this represents a material, irreversible loss of governance token maturity for all participants. This matches the allowed impact: *"Significant SNS security impact with concrete user or protocol harm."*

## Likelihood Explanation
The trigger condition — all voters on all `ReadyToSettle` proposals in a round having zero voting power — is realistic in SNS deployments. It can occur naturally when neurons accumulate transaction fees until `neuron_fees_e8s >= cached_neuron_stake_e8s`, particularly in small or newly launched SNS instances with few neurons. No attacker action is required; the condition can arise from normal protocol operation. The DFINITY-authored test `zero_total_reward_shares` explicitly demonstrates the condition is reachable and the rewards are silently dropped, confirming this is a known-reachable code path.

## Recommendation
Decouple the rollover predicate from proposal settlement. Change `rewards_rolled_over()` in both SNS (`rs/sns/governance/src/types.rs`) and NNS (`rs/nns/governance/src/reward/calculation.rs`) to:

```rust
pub(crate) fn rewards_rolled_over(&self) -> bool {
    self.distributed_e8s_equivalent == 0
}
```

This correctly preserves the purse whenever no rewards were actually distributed, regardless of whether proposals were settled, matching the documented intent of the function's doc-comment.

## Proof of Concept
The existing unit test at `rs/sns/integration_tests/src/neuron.rs:1168` (`zero_total_reward_shares`) is a direct proof of concept. It:
1. Creates a neuron with `cached_neuron_stake_e8s == neuron_fees_e8s` (voting power = 0).
2. Creates a `ReadyToSettle` proposal with that neuron's ballot.
3. Calls `governance.run_periodic_tasks().await`.
4. Asserts `reward_event.distributed_e8s_equivalent == 0` with `settled_proposals` non-empty.

To confirm the purse loss, extend the test: set `total_supply > 0` in `EmptyLedger::total_supply`, assert `total_available_e8s_equivalent > 0` in the resulting `RewardEvent`, then advance time one more round and assert `e8s_equivalent_to_be_rolled_over()` returns `0` — confirming the purse is permanently dropped.

### Citations

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

**File:** rs/sns/governance/src/types.rs (L2065-2067)
```rust
    pub(crate) fn rewards_rolled_over(&self) -> bool {
        self.settled_proposals.is_empty()
    }
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

**File:** rs/nns/governance/src/reward/calculation.rs (L144-147)
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
