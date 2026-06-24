### Title
SNS Governance Lacks Voting Power Spike Detection, Enabling Immediate Early Execution by Sudden Large Stakers - (File: rs/sns/governance/src/governance.rs)

### Summary
The SNS governance canister has no protection against a sudden concentration of voting power being used to immediately early-execute proposals. The NNS governance recently added a `VotingPowerSnapshots` mechanism (Proposal 137252) that detects when current voting power exceeds 1.5× the historical minimum and falls back to a previous snapshot for ballot creation. The SNS governance's `compute_ballots_for_new_proposal` has no equivalent protection, allowing an attacker who acquires a majority of SNS tokens to stake, create a proposal, vote YES, and trigger immediate early execution — all within a short window.

### Finding Description

The NNS governance's `compute_ballots_for_standard_proposal` checks `VOTING_POWER_SNAPSHOTS` before creating ballots: [1](#0-0) 

If the current total potential voting power exceeds 1.5× the minimum in the rolling 7-day snapshot window, it falls back to the previous snapshot's ballots, preventing the spike from controlling the proposal: [2](#0-1) 

The SNS governance's `compute_ballots_for_new_proposal` has no such check. It simply iterates all neurons at the moment of proposal creation and assigns voting power directly: [3](#0-2) 

After each vote, `register_vote` calls `process_proposal`, which evaluates `early_decision()`: [4](#0-3) 

`early_decision()` checks only whether the current tally exceeds the configured threshold — with no time-delay or spike guard: [5](#0-4) 

`can_make_decision` returns `true` immediately upon an absolute majority, with no minimum elapsed time: [6](#0-5) 

### Impact Explanation

An attacker who acquires a majority of an SNS's governance tokens can:

1. Stake them with the minimum dissolve delay required to vote (`neuron_minimum_dissolve_delay_to_vote_seconds`).
2. Create a malicious proposal (e.g., `TransferSnsTreasuryFunds` to drain the treasury, or `UpgradeSnsControlledCanister` to install backdoored Wasm).
3. Vote YES immediately.
4. The proposal is early-executed in the same ingress round — before any other neuron holder can react.
5. Begin dissolving the neuron to eventually recover the staked tokens.

High-impact SNS proposal types that can be immediately executed include treasury drains, canister upgrades, and nervous system parameter changes. The SNS ballot structure confirms voting power is fixed at proposal creation time with no historical baseline: [7](#0-6) 

### Likelihood Explanation

- Acquiring a majority of SNS tokens is feasible for SNS instances with low market cap, concentrated initial distributions, or tokens tradeable on DEXes.
- The SNS swap mechanism can result in a single large participant holding near-majority stake.
- No time delay, no spike detection, and no historical baseline exist in SNS governance to slow or block this attack.
- The NNS governance team explicitly recognized this risk and deployed a fix (Proposal 137252, 2025-07-06): [8](#0-7) 

The SNS governance has received no equivalent protection.

### Recommendation

Port the `VotingPowerSnapshots` mechanism from NNS governance to SNS governance:

1. Add a recurring timer task in SNS governance (analogous to `SnapshotVotingPowerTask`) that records a daily snapshot of total voting power.
2. In `compute_ballots_for_new_proposal`, compare the current total voting power against the rolling minimum. If it exceeds the spike threshold (e.g., 1.5×), use the previous snapshot's voting power distribution for ballot creation.
3. Alternatively, enforce a minimum elapsed time (e.g., one round) before `early_decision` can trigger execution, giving existing neuron holders a window to react. [9](#0-8) 

### Proof of Concept

**Attacker-controlled entry path (unprivileged ingress sender):**

```
// Step 1: Attacker transfers SNS tokens to their neuron subaccount via the SNS ledger
icrc1_transfer(to: governance_canister / attacker_subaccount, amount: majority_stake)

// Step 2: Claim the neuron and set dissolve delay to the minimum required to vote
manage_neuron(ClaimOrRefresh { ... })
manage_neuron(IncreaseDissolveDelay { additional_dissolve_delay_seconds: min_delay })

// Step 3: Create a malicious proposal (e.g., drain treasury)
make_proposal(TransferSnsTreasuryFunds { to: attacker_account, amount: treasury_balance })
// -> compute_ballots_for_new_proposal assigns attacker's neuron majority of total voting power

// Step 4: Vote YES — triggers process_proposal -> early_decision -> immediate execution
manage_neuron(RegisterVote { proposal_id, vote: Yes })
// -> early_decision() returns Vote::Yes (attacker has >50% of total)
// -> proposal is executed immediately, treasury drained

// Step 5: Start dissolving neuron to recover tokens after dissolve delay
manage_neuron(StartDissolving { })
```

The SNS `compute_ballots_for_new_proposal` has no historical baseline check, so the attacker's freshly staked majority is accepted at face value: [10](#0-9) 

Contrast with the NNS, where the same scenario would be caught by `previous_ballots_if_voting_power_spike_detected` and the attacker's neuron would not appear in the ballots at all: [11](#0-10)

### Citations

**File:** rs/nns/governance/src/governance.rs (L5504-5524)
```rust
        // Check if there is a voting power spike. If there is, then the return value here
        // will be `Some(...)`.
        let maybe_previous_ballots_if_voting_power_spike_detected = VOTING_POWER_SNAPSHOTS
            .with_borrow(|snapshots| {
                snapshots.previous_ballots_if_voting_power_spike_detected(
                    current_voting_power_snapshot.total_potential_voting_power(),
                    now_seconds,
                )
            });

        let (voting_power_snapshot, previous_ballots_timestamp_seconds) =
            match maybe_previous_ballots_if_voting_power_spike_detected {
                // This is the extraordinary case - we have a voting power spike, and we
                // need to use the previous snapshot.
                Some((previous_snapshot_timestamp, previous_snapshot)) => {
                    (previous_snapshot, Some(previous_snapshot_timestamp))
                }
                // This is the normal case - we have no voting power spike, so we use the
                // current snapshot.
                None => (current_voting_power_snapshot, None),
            };
```

**File:** rs/nns/governance/src/governance/voting_power_snapshots.rs (L19-21)
```rust
/// power is more than this multiplier times the minimum total voting power in the snapshots,
/// then we consider it a spike.
const MULTIPLIER_THRESHOLD_FOR_VOTING_POWER_SPIKE: f64 = 1.5;
```

**File:** rs/nns/governance/src/governance/voting_power_snapshots.rs (L157-167)
```rust
    pub(crate) fn previous_ballots_if_voting_power_spike_detected(
        &self,
        total_potential_voting_power: u64,
        now_seconds: TimestampSeconds,
    ) -> Option<(TimestampSeconds, VotingPowerSnapshot)> {
        // Step 0: skip the check in test mode when the snapshots are not yet full. Otherwise it
        // would be difficult to get around the spike detection in tests, and a lot of test setups
        // involve creating a lot of voting power.
        if cfg!(feature = "test") && self.voting_power_totals.len() < MAX_VOTING_POWER_SNAPSHOTS {
            return None;
        }
```

**File:** rs/sns/governance/src/governance.rs (L3931-3944)
```rust
        Governance::cast_vote_and_cascade_follow(
            proposal_id,
            neuron_id,
            vote,
            function_id,
            &self.function_followee_index,
            &self.topic_follower_index,
            &self.proto.neurons,
            now_seconds,
            &mut proposal.ballots,
            proposal_topic.unwrap_or_default(),
        );

        self.process_proposal(proposal_id.id);
```

**File:** rs/sns/governance/src/governance.rs (L5225-5227)
```rust
    /// Computes the total potential voting power of the governance canister and ballots.
    fn compute_ballots_for_new_proposal(&self) -> Result<(u64, BTreeMap<String, Ballot>), String> {
        let now_seconds = self.env.now();
```

**File:** rs/sns/governance/src/governance.rs (L5255-5280)
```rust
        for (k, v) in self.proto.neurons.iter() {
            // If this neuron is eligible to vote, record its
            // voting power at the time of proposal creation (now).
            if v.dissolve_delay_seconds(now_seconds) < min_dissolve_delay_for_vote {
                // Not eligible due to dissolve delay.
                continue;
            }

            let voting_power = v.voting_power(
                now_seconds,
                max_dissolve_delay,
                max_age_bonus,
                max_dissolve_delay_bonus_percentage,
                max_age_bonus_percentage,
            );

            total_power += voting_power as u128;
            electoral_roll.insert(
                k.clone(),
                Ballot {
                    vote: Vote::Unspecified as i32,
                    voting_power,
                    cast_timestamp_seconds: 0,
                },
            );
        }
```

**File:** rs/sns/governance/src/proposal.rs (L2310-2341)
```rust
    /// Returns true if a decision can be made right now to adopt or reject the proposal.
    /// The proposal must be tallied prior to calling this method.
    pub fn can_make_decision(&self, now_seconds: u64) -> bool {
        debug_assert!(self.latest_tally.is_some());
        let Some(tally) = &self.latest_tally else {
            return false;
        };
        // Even when a proposal's deadline has not passed, a proposal is
        // adopted if strictly more than half of the votes are 'yes' and
        // rejected if at least half of the votes are 'no'. The conditions
        // are described as below to avoid overflow. In the absence of overflow,
        // the below is equivalent to (2 * yes > total) || (2 * no >= total).
        let absolute_majority = self.early_decision() != Vote::Unspecified;
        let expired = !self.accepts_vote(now_seconds);
        let decision_reason = match (absolute_majority, expired) {
            (true, true) => "majority and expiration",
            (true, false) => "majority",
            (false, true) => "expiration",
            (false, false) => return false,
        };
        log!(
            INFO,
            "{}Proposal {} decided, thanks to {}. Tally at decision time: {:?}",
            log_prefix(),
            self.id
                .as_ref()
                .map_or("unknown".to_string(), |i| format!("{}", i.id)),
            decision_reason,
            tally
        );
        true
    }
```

**File:** rs/sns/governance/src/proposal.rs (L2350-2364)
```rust
    pub fn early_decision(&self) -> Vote {
        let tally = &self
            .latest_tally
            .as_ref()
            .expect("expected latest_tally to not be None");

        let minimum_yes_proportion_of_exercised = self.minimum_yes_proportion_of_exercised();

        Self::majority_decision(
            tally.yes,
            tally.no,
            tally.total,
            minimum_yes_proportion_of_exercised,
        )
    }
```

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L1265-1269)
```rust
    /// The voting power associated with the ballot. The voting power of a ballot
    /// associated with a neuron and a proposal is set at the proposal's creation
    /// time to the neuron's voting power at that time.
    #[prost(uint64, tag = "2")]
    pub voting_power: u64,
```

**File:** rs/nns/governance/CHANGELOG.md (L453-461)
```markdown
# 2025-07-06: Proposal 137252

http://dashboard.internetcomputer.org/proposal/137252

## Added

* Add a metric for the nubmer of spawning neurons.
* Use a previous voting power snapshot to create ballots if a voting power spike is detected.

```

**File:** rs/nns/governance/src/timer_tasks/snapshot_voting_power.rs (L9-16)
```rust
/// A task to snapshot the voting power every day, so that the snapshot can be used to disable
/// early adoption of proposals if such proposals have unusually high voting power.
pub(super) struct SnapshotVotingPowerTask {
    governance: &'static LocalKey<RefCell<Governance>>,
    snapshots: &'static LocalKey<RefCell<VotingPowerSnapshots>>,
}

const VOTING_POWER_SNAPSHOT_INTERVAL: Duration = Duration::from_secs(60 * 60 * 24);
```
