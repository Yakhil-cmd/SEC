### Title
Stale Voting Power Snapshot Grants Ballot Rights to Neurons That No Longer Meet Eligibility Criteria - (`rs/nns/governance/src/governance/voting_power_snapshots.rs`, `rs/nns/governance/src/governance.rs`)

---

### Summary

When the NNS governance canister detects a "voting power spike" at proposal creation time, it falls back to a **previous historical snapshot** to create ballots. Any neuron present in that old snapshot receives a ballot — and can vote — even if, by the time of voting, the neuron no longer meets the current eligibility criteria (e.g., its dissolve delay has since dropped below the minimum, or its stake has been fully disbursed). The `register_vote` function does not re-validate current neuron eligibility; it only checks whether a ballot exists in the proposal.

---

### Finding Description

The NNS governance canister implements a "voting power spike" detection mechanism. When a new proposal is created, if the current total potential voting power exceeds 1.5× the minimum total potential voting power across the last 7 daily snapshots, the system uses the **oldest snapshot with minimum voting power** to create ballots instead of the current state. [1](#0-0) 

The snapshot used for ballots can be up to **7 days old** (one snapshot per day, 7 retained). [2](#0-1) 

When a neuron later calls `register_vote`, the only eligibility check performed is whether the neuron's ID appears in the proposal's `ballots` map (i.e., it was in the snapshot). There is **no re-check** of the neuron's current dissolve delay, stake, or activity status at vote time: [3](#0-2) 

The snapshot is computed from `with_active_neurons_iter_sections`, which filters by `is_inactive` and `dissolve_delay_seconds >= min_dissolve_delay_seconds` **at snapshot time**: [4](#0-3) 

However, between the snapshot timestamp and the time of voting (which can be days later), a neuron's state can change:

1. A neuron that was `NotDissolving` with a 2-week dissolve delay (the new minimum under Mission 70) could start dissolving and reach `Dissolved` state within the proposal's voting window.
2. A neuron could disburse its stake to zero after the snapshot but before voting.

In both cases, the neuron retains a ballot with non-zero voting power from the old snapshot and can still cast a vote.

The `SnapshotVotingPowerTask` runs daily and the spike detection uses the snapshot with **minimum** total potential voting power among the last 7: [5](#0-4) 

The `previous_ballots_timestamp_seconds` field on `ProposalData` records when this occurred, but no runtime guard prevents the stale-ballot holder from voting: [6](#0-5) 

---

### Impact Explanation

A neuron owner whose neuron was eligible at snapshot time but has since become ineligible (dissolved, disbursed, or fallen below minimum dissolve delay) can still cast a vote with the voting power recorded in the stale snapshot. This is a **governance authorization bug**: the ballot system grants voting rights based on historical state rather than current state, allowing a neuron that no longer has skin in the game to influence proposal outcomes.

Under Mission 70, the minimum dissolve delay to vote was reduced to 2 weeks: [7](#0-6) 

This makes the window of exploitation larger: a neuron with exactly 2 weeks of dissolve delay at snapshot time could start dissolving immediately after the snapshot, and within the proposal's voting period (which can be days), reach dissolved state — yet still hold a valid ballot.

---

### Likelihood Explanation

- The spike detection path is triggered when total voting power grows by more than 1.5× relative to the minimum snapshot. This is an unusual but reachable condition (e.g., a large staker creates a neuron, as demonstrated in the integration test `test_proposal_with_voting_power_spike`).
- Under Mission 70, the minimum dissolve delay is 2 weeks, making it feasible for a neuron to dissolve within a proposal's voting window after being captured in a snapshot.
- The attacker entry path is a standard unprivileged ingress call to `manage_neuron` with `RegisterVote` — no special privileges required.
- The attacker must have controlled a neuron that was eligible at snapshot time. This is a realistic scenario for any NNS staker. [8](#0-7) 

---

### Recommendation

In `register_vote` (NNS governance), when `proposal.previous_ballots_timestamp_seconds` is set (indicating a spike-based ballot), add a check that the neuron still meets current eligibility criteria (active, dissolve delay ≥ minimum) before allowing the vote to be cast. Alternatively, at vote time, verify the neuron's current `deciding_voting_power` is non-zero before accepting the ballot. [9](#0-8) 

---

### Proof of Concept

1. The NNS has been running for 7+ days with stable voting power. A large staker creates a new neuron with 1,000,000 ICP, causing a voting power spike (>1.5× the minimum snapshot).
2. A proposal P is created. Because a spike is detected, ballots are created from the snapshot taken ~7 days ago. Neuron A (dissolve delay = 2 weeks at snapshot time) receives a ballot with significant voting power.
3. Immediately after the snapshot, Neuron A's owner starts dissolving. Within 2 weeks, the neuron reaches `Dissolved` state.
4. Proposal P is still open (voting period has not expired). Neuron A's owner calls `manage_neuron` → `RegisterVote` on proposal P.
5. `register_vote` finds Neuron A's ballot in `proposal.ballots`, sees `vote == Unspecified`, and accepts the vote — despite the neuron being dissolved and ineligible at vote time.
6. The vote is counted with the full voting power from the stale snapshot, potentially swaying the outcome of proposal P. [10](#0-9)

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

**File:** rs/nns/governance/src/governance.rs (L5585-5657)
```rust
    async fn register_vote(
        &mut self,
        neuron_id: &NeuronId,
        caller: &PrincipalId,
        register_vote: &manage_neuron::RegisterVote,
    ) -> Result<(), GovernanceError> {
        let now_seconds = self.env.now();
        let voting_period_seconds = self.voting_period_seconds();

        let is_neuron_authorized_to_vote =
            self.with_neuron(neuron_id, |neuron| neuron.is_authorized_to_vote(caller))?;
        // Check that the caller is authorized, i.e., either the
        // controller or a registered hot key.
        if !is_neuron_authorized_to_vote {
            return Err(GovernanceError::new_with_message(
                ErrorType::NotAuthorized,
                "Caller is not authorized to vote for neuron.",
            ));
        }

        let manage_neuron::RegisterVote { proposal, vote } = register_vote;

        let proposal_id = proposal.as_ref().ok_or_else(||
            // Proposal not specified.
            GovernanceError::new_with_message(ErrorType::PreconditionFailed, "Vote must include a proposal id."))?;
        let proposal = self
            .heap_data
            .proposals
            .get_mut(&proposal_id.id)
            .ok_or_else(||
            // Proposal not found.
            GovernanceError::new_with_message(ErrorType::NotFound, "Can't find proposal."))?;
        let topic = proposal.topic();

        let vote = Vote::try_from(*vote).unwrap_or(Vote::Unspecified);
        if vote == Vote::Unspecified {
            // Invalid vote specified, i.e., not yes or no.
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Invalid vote specified.",
            ));
        }

        // Check if the proposal is still open for voting.
        let voting_period_seconds = voting_period_seconds(topic);
        let accepts_vote = proposal.accepts_vote(now_seconds, voting_period_seconds);
        if !accepts_vote {
            // Deadline has passed, so the proposal cannot be voted on
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Proposal deadline has passed.",
            ));
        }

        let neuron_ballot = proposal.ballots.get_mut(&neuron_id.id).ok_or_else(||
            // This neuron is not eligible to vote on this proposal.
            GovernanceError::new_with_message(ErrorType::NotAuthorized, "Neuron not authorized to vote on proposal."))?;
        if neuron_ballot.vote != (Vote::Unspecified as i32) {
            // Already voted.
            return Err(GovernanceError::new_with_message(
                ErrorType::NeuronAlreadyVoted,
                "Neuron already voted on proposal.",
            ));
        }

        self.cast_vote_and_cascade_follow(
            // Actually update the ballot, including following.
            *proposal_id,
            *neuron_id,
            vote,
            topic,
        )
        .await;
```

**File:** rs/nns/governance/src/governance/voting_power_snapshots.rs (L16-25)
```rust
/// The maximum number of voting power snapshots to keep.
const MAX_VOTING_POWER_SNAPSHOTS: u64 = 7;
/// The multiplier used to define what is a "voting power spike": if the current total voting
/// power is more than this multiplier times the minimum total voting power in the snapshots,
/// then we consider it a spike.
const MULTIPLIER_THRESHOLD_FOR_VOTING_POWER_SPIKE: f64 = 1.5;
/// The maximum staleness of a voting power snapshot. This is usually not needed since
/// the snapshots should be added frequently. However, we do not want to use a snapshot that is too
/// old, in the event of a failure in taking the snapshots.
const MAXIMUM_STALENESS_SECONDS: u64 = ONE_MONTH_SECONDS * 3;
```

**File:** rs/nns/governance/src/governance/voting_power_snapshots.rs (L122-151)
```rust
    fn totals_entry_with_minimum_total_potential_voting_power_if_voting_power_spiked(
        &self,
        now_seconds: TimestampSeconds,
        current_total_potential_voting_power: u64,
    ) -> Option<(TimestampSeconds, VotingPowerTotal)> {
        let (
            timestamp_with_minimum_total_potential_voting_power,
            totals_with_minimum_total_potential_voting_power,
        ) = self
            .voting_power_totals
            .iter()
            .filter(|(created_at, _)| {
                let age = now_seconds - created_at;
                age <= MAXIMUM_STALENESS_SECONDS
            })
            .min_by_key(|(_, snapshot)| snapshot.total_potential_voting_power)?;

        let voting_power_spike_detected = (current_total_potential_voting_power as f64)
            > (totals_with_minimum_total_potential_voting_power.total_potential_voting_power
                as f64)
                * MULTIPLIER_THRESHOLD_FOR_VOTING_POWER_SPIKE;
        if voting_power_spike_detected {
            Some((
                timestamp_with_minimum_total_potential_voting_power,
                totals_with_minimum_total_potential_voting_power,
            ))
        } else {
            None
        }
    }
```

**File:** rs/nns/governance/src/neuron_store/voting_power.rs (L144-159)
```rust
        let mut process_neuron = |neuron: &Neuron| {
            if neuron.is_inactive(now_seconds)
                || neuron.dissolve_delay_seconds(now_seconds) < min_dissolve_delay_seconds
            {
                return;
            }

            let (potential_voting_power, deciding_voting_power) =
                neuron.potential_and_deciding_voting_power(voting_power_economics, now_seconds);
            // We don't handle overflow here, as in `get_voting_power_as_u64` below,
            // the input arguments bigger than u64::MAX will result in an error.
            total_deciding_voting_power =
                total_deciding_voting_power.saturating_add(deciding_voting_power as u128);
            total_potential_voting_power =
                total_potential_voting_power.saturating_add(potential_voting_power as u128);
            voting_power_map.insert(neuron.id().id, deciding_voting_power);
```

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L1251-1255)
```text
  // When a voting power spike is detected, ballots are created using a previous snapshot of the
  // voting power, and this field indicates the timestamp at which the snapshot was taken. This
  // field should not be set in normal circumstances, and if it is set, it is an indicator that a
  // bug might have caused the voting power spike.
  optional uint64 previous_ballots_timestamp_seconds = 24;
```

**File:** rs/nns/governance/src/network_economics.rs (L282-283)
```rust
    pub const MISSION_70_DEFAULT_NEURON_MINIMUM_DISSOLVE_DELAY_TO_VOTE_SECONDS: u64 =
        14 * ONE_DAY_SECONDS;
```

**File:** rs/nns/integration_tests/src/governance_proposals.rs (L68-99)
```rust
#[test]
fn test_proposal_with_voting_power_spike() {
    let state_machine = state_machine_builder_for_nns_tests().build();
    let nns_init_payloads = NnsInitPayloadsBuilder::new().with_test_neurons().build();
    setup_nns_canisters(&state_machine, nns_init_payloads);

    // For a few days, proposals made by a neuron which wields a lot of voting power should be
    // executed immediately.
    for _ in 0..10 {
        let proposal_id = make_motion_proposal(
            &state_machine,
            *TEST_NEURON_1_OWNER_PRINCIPAL,
            NeuronId::from_u64(TEST_NEURON_1_ID),
        );
        assert!(is_proposal_executed(&state_machine, proposal_id));

        state_machine.advance_time(Duration::from_secs(60 * 60 * 24));
    }

    // Now we create a super powerful neuron, which will cause a spike in voting power compared to
    // previous days.
    let super_powerful_neuron_id = nns_create_super_powerful_neuron(
        &state_machine,
        *TEST_NEURON_3_OWNER_PRINCIPAL,
        Tokens::from_tokens(1_000_000).unwrap(),
    );
    let proposal_id = make_motion_proposal(
        &state_machine,
        *TEST_NEURON_3_OWNER_PRINCIPAL,
        super_powerful_neuron_id,
    );
    assert!(!is_proposal_executed(&state_machine, proposal_id));
```
