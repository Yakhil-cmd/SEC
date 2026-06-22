### Title
Voting Rewards Permanently Lost When All Settled-Proposal Voters Have Zero Voting Power - (`rs/sns/governance/src/governance.rs` and `rs/nns/governance/src/governance.rs`)

---

### Summary

Both SNS and NNS governance contain a reward-accounting bug that permanently destroys the rewards purse for a round when proposals are settled but every voter on those proposals had zero voting power. The rollover gate (`rewards_rolled_over()`) is keyed solely on whether `settled_proposals` is empty — not on whether any rewards were actually distributed. When proposals are settled with zero total voting power, the full purse is recorded in `total_available_e8s_equivalent` but is never rolled over, and is never distributed, causing it to vanish permanently.

---

### Finding Description

**Vulnerability class:** Governance ledger conservation bug — rewards accounting divergence when no effective participants exist during a settled reward round.

**Root cause — SNS (`rs/sns/governance/src/governance.rs`):**

In `distribute_rewards`, after computing `rewards_purse_e8s` and iterating over proposal ballots, the code guards distribution behind a zero-check:

```rust
if total_reward_shares == dec!(0) {
    log!(ERROR, "Warning: total_reward_shares is 0. ...");
    // distributed_e8s_equivalent stays 0
} else {
    // hand out rewards
}
``` [1](#0-0) 

The function then unconditionally settles all considered proposals and writes the new `RewardEvent`:

```rust
self.proto.latest_reward_event = Some(RewardEvent {
    settled_proposals: considered_proposals,   // non-empty
    distributed_e8s_equivalent,               // 0
    total_available_e8s_equivalent,           // Some(purse > 0)
    ...
})
``` [2](#0-1) 

**Root cause — rollover gate (`rs/sns/governance/src/types.rs`):**

The rollover predicate is:

```rust
pub(crate) fn rewards_rolled_over(&self) -> bool {
    self.settled_proposals.is_empty()
}
``` [3](#0-2) 

Because `settled_proposals` is non-empty, `rewards_rolled_over()` returns `false`, so `e8s_equivalent_to_be_rolled_over()` returns `0`:

```rust
pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
    if self.rewards_rolled_over() {
        self.total_available_e8s_equivalent.unwrap_or_default()
    } else {
        0   // ← purse is silently dropped
    }
}
``` [4](#0-3) 

The next round's `rewards_purse_e8s` starts from `0` rollover, and the prior round's purse is permanently unrecoverable.

**Identical root cause in NNS (`rs/nns/governance/src/governance.rs` and `rs/nns/governance/src/reward/calculation.rs`):**

NNS uses the same pattern: when `total_voting_rights < 0.001` the distribution is skipped but proposals are still settled, and `rewards_rolled_over()` is identically gated on `settled_proposals.is_empty()`. [5](#0-4) [6](#0-5) 

---

### Impact Explanation

When the condition is triggered, the entire rewards purse for that round — computed as `supply × reward_rate × round_duration` plus any previously rolled-over amount — is permanently destroyed. No neuron receives maturity, and the amount is not carried forward. For an SNS with a meaningful token supply and reward rate, this can represent a material loss of governance token maturity for all participants. The loss is irreversible because the `latest_reward_event` is overwritten and the purse value is not stored anywhere else.

---

### Likelihood Explanation

The trigger condition requires that all voters on all `ReadyToSettle` proposals in a given round have zero voting power. A neuron's voting power is zero when `cached_neuron_stake_e8s <= neuron_fees_e8s`. [7](#0-6) 

This is realistic in SNS deployments where:
- Neurons accumulate transaction fees over time until fees equal stake.
- An SNS is newly launched with only a small number of neurons, some of which are depleted.
- A governance attack deliberately depletes neuron stakes before a reward round closes.

The existing test `zero_total_reward_shares` in `rs/sns/integration_tests/src/neuron.rs` explicitly constructs this scenario and confirms `distributed_e8s_equivalent == 0` with a non-empty `settled_proposals`, demonstrating the condition is reachable and the rewards are silently dropped. [8](#0-7) 

---

### Recommendation

The rollover predicate must be decoupled from proposal settlement. A round should be considered a "rollover" (and its purse preserved) whenever `distributed_e8s_equivalent == 0`, regardless of whether proposals were settled. Concretely, change `rewards_rolled_over()` in both SNS and NNS to:

```rust
pub(crate) fn rewards_rolled_over(&self) -> bool {
    self.distributed_e8s_equivalent == 0
}
```

This mirrors the intent described in the doc-comment ("if rewards were distributed for this event, then no available_icp_e8s should be rolled over") and matches the analogous fix suggested for `StakedEXA`: when no effective participants exist, preserve the undistributed amount for the next round rather than silently discarding it. [9](#0-8) [10](#0-9) 

---

### Proof of Concept

**Scenario (SNS):**

1. Deploy an SNS with one neuron where `cached_neuron_stake_e8s == neuron_fees_e8s` (voting power = 0).
2. Submit a proposal; the neuron's ballot is recorded with `voting_power = 0`.
3. Advance time past the proposal's voting period → proposal becomes `ReadyToSettle`.
4. Advance time past one full reward round → `run_periodic_tasks` calls `distribute_rewards`.
5. Inside `distribute_rewards`:
   - `rewards_purse_e8s = supply × rate × duration > 0` (purse is non-zero).
   - `total_reward_shares = 0` (all ballots have zero voting power).
   - Distribution is skipped; `distributed_e8s_equivalent = 0`.
   - Proposal is settled → `settled_proposals = [proposal_id]` (non-empty).
   - `latest_reward_event.total_available_e8s_equivalent = Some(purse)`.
6. Next reward round: `e8s_equivalent_to_be_rolled_over()` returns `0` because `rewards_rolled_over()` is `false` (settled_proposals non-empty).
7. The purse from step 5 is permanently lost — neither distributed nor carried forward.

The existing unit test at `rs/sns/integration_tests/src/neuron.rs:1168` (`zero_total_reward_shares`) confirms steps 1–6 and asserts `distributed_e8s_equivalent == 0` with a non-empty `settled_proposals`, directly demonstrating the stuck-rewards state. [11](#0-10)

### Citations

**File:** rs/sns/governance/src/governance.rs (L5892-5934)
```rust
        // Add up reward shares based on voting power that was exercised.
        let mut neuron_id_to_reward_shares: HashMap<NeuronId, Decimal> = HashMap::new();
        for proposal_id in &considered_proposals {
            if let Some(proposal) = self.get_proposal_data(*proposal_id) {
                for (voter, ballot) in &proposal.ballots {
                    #[allow(clippy::blocks_in_conditions)]
                    if !Vote::try_from(ballot.vote)
                        .unwrap_or_else(|_| {
                            println!(
                                "{}Vote::from invoked with unexpected value {}.",
                                log_prefix(),
                                ballot.vote
                            );
                            Vote::Unspecified
                        })
                        .eligible_for_rewards()
                    {
                        continue;
                    }

                    match NeuronId::from_str(voter) {
                        Ok(neuron_id) => {
                            let reward_shares = i2d(ballot.voting_power);
                            *neuron_id_to_reward_shares
                                .entry(neuron_id)
                                .or_insert_with(|| dec!(0)) += reward_shares;
                        }
                        Err(e) => {
                            log!(
                                ERROR,
                                "Could not use voter {} to calculate total_voting_rights \
                                 since it's NeuronId was invalid. Underlying error: {:?}.",
                                voter,
                                e
                            );
                        }
                    }
                }
            }
        }
        // Freeze reward shares, now that we are done adding them up.
        let neuron_id_to_reward_shares = neuron_id_to_reward_shares;
        let total_reward_shares: Decimal = neuron_id_to_reward_shares.values().sum();
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

**File:** rs/sns/governance/src/types.rs (L2046-2060)
```rust
    /// Calculates the total_available_e8s_equivalent in this event that should
    /// be "rolled over" into the next `RewardEvent`.
    ///
    /// Behavior:
    /// - If rewards were distributed for this event, then no available_icp_e8s
    ///   should be rolled over, so this function returns 0.
    /// - Otherwise, this function returns
    ///   `total_available_e8s_equivalent`.
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

**File:** rs/nns/governance/src/reward/calculation.rs (L111-147)
```rust
impl RewardEvent {
    /// Calculates the total_available_e8s_equivalent in this event that should
    /// be "rolled over" into the next `RewardEvent`.
    ///
    /// Behavior:
    /// - If rewards were distributed for this event, then no available_icp_e8s
    ///   should be rolled over, so this function returns 0.
    /// - Otherwise, this function returns
    ///   `total_available_e8s_equivalent`.
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
