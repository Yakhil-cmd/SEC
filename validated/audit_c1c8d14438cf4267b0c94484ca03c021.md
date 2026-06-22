### Title
No Deduplication of Identical Proposals Enables Vote-Splitting and Camouflaged Malicious Proposals in SNS Governance - (File: rs/sns/governance/src/governance.rs)

### Summary
The SNS governance `make_proposal` function contains no check for duplicate or identical open proposals. Any neuron holder with stake ≥ `reject_cost_e8s` can submit arbitrarily many identical (or near-identical) proposals, splitting votes across them or camouflaging a malicious variant among legitimate-looking copies. The codebase itself explicitly acknowledges this gap.

### Finding Description
In `rs/sns/governance/src/governance.rs`, the `make_proposal` function validates the proposer's dissolve delay, stake, and the global ballot-count cap, but performs **no content-based deduplication** against existing open proposals. [1](#0-0) 

The only economic deterrent is the upfront deduction of `reject_cost_e8s` from the proposer's neuron: [2](#0-1) 

This fee is **returned to the neuron if the proposal is adopted**, meaning an attacker who controls enough voting power to pass proposals bears no net cost. Even when the fee is lost (rejected proposals), a low `reject_cost_e8s` (configurable per SNS) makes the attack cheap.

The codebase explicitly acknowledges the absence of deduplication in an integration test comment: [3](#0-2) 

By contrast, the NNS governance does enforce single-open-proposal constraints for specific high-impact types (e.g., `CreateServiceNervousSystem` returns an error if "another open" proposal of that type exists), but SNS `Motion`, `TransferSnsTreasuryFunds`, `ExecuteGenericNervousSystemFunction`, and other proposal types have no such guard. [4](#0-3) 

### Impact Explanation
Two concrete impacts:

1. **Vote splitting (griefing)**: An attacker submits N identical `Motion` proposals. Voters following automated systems or voting by proposal ID may distribute votes across all N copies instead of concentrating them on the legitimate one, preventing quorum on the real proposal.

2. **Camouflaged malicious action**: An attacker submits several proposals that are identical in `title`, `summary`, and `url` but differ in one field of the `action` (e.g., a `TransferSnsTreasuryFunds` with a different `to_principal` or `amount_e8s`). Voters who do not inspect the on-chain action payload carefully may vote `Yes` on the malicious variant. This can result in unauthorized treasury transfers or canister upgrades to attacker-controlled code. [5](#0-4) 

### Likelihood Explanation
- **Entry path**: Any unprivileged principal who holds an SNS neuron with `stake_e8s >= reject_cost_e8s` and `dissolve_delay >= neuron_minimum_dissolve_delay_to_vote_seconds` can call `manage_neuron` → `MakeProposal` repeatedly with identical content. No privileged role is required.
- **Cost**: `reject_cost_e8s` per fake proposal, which is configurable by the DAO and may be set to a low value. If the attacker's neuron has enough voting power to adopt proposals, the fee is refunded.
- **Automation**: The attack is fully automatable — the attacker simply re-submits the same `Proposal` struct multiple times via ingress messages to the SNS governance canister. [6](#0-5) 

### Recommendation
1. **Content-hash deduplication**: Before inserting a new proposal, compute a canonical hash of the `action` field and reject submission if an open proposal with the same action hash already exists.
2. **Per-neuron open-proposal cap**: Limit the number of simultaneously open proposals per proposer neuron (separate from the global `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS` cap).
3. **Increase minimum `reject_cost_e8s`**: Raise the default and minimum configurable value so that spamming many proposals carries a meaningful economic cost even for large neuron holders. [7](#0-6) 

### Proof of Concept
1. Deploy or interact with an SNS whose `reject_cost_e8s` is low (e.g., 1 SNS token).
2. Obtain an SNS neuron with stake ≥ `reject_cost_e8s` and sufficient dissolve delay.
3. Construct a `TransferSnsTreasuryFunds` proposal transferring 1000 ICP to address A (the legitimate proposal).
4. Submit it via `manage_neuron` → `MakeProposal`. Note the returned `proposal_id` (e.g., 42).
5. Immediately submit 4 more proposals with identical `title`/`summary`/`url` but `to_principal` set to attacker-controlled address B. These receive IDs 43–46.
6. Announce proposal 42 publicly as the "real" proposal. Voters who check only the title/summary and vote on ID 43–46 by mistake will authorize a transfer to address B.
7. If the attacker controls >50% of voting power, they can adopt proposal 43 (malicious) while proposal 42 (legitimate) fails to reach quorum due to vote splitting. [8](#0-7)

### Citations

**File:** rs/sns/governance/src/governance.rs (L3457-3462)
```rust
    pub async fn make_proposal(
        &mut self,
        proposer_id: &NeuronId,
        caller: &PrincipalId,
        proposal: &Proposal,
    ) -> Result<ProposalId, GovernanceError> {
```

**File:** rs/sns/governance/src/governance.rs (L3489-3547)
```rust
        let reject_cost_e8s = nervous_system_parameters
            .reject_cost_e8s
            .expect("NervousSystemParameters must have reject_cost_e8s");

        // Before actually modifying anything, we first make sure that
        // the neuron is allowed to make this proposal and create the
        // electoral roll.
        //
        // Find the proposing neuron.
        let proposer = self.get_neuron_result(proposer_id)?;

        // === Validation
        //
        // Check that the caller is authorized to make a proposal
        proposer.check_authorized(caller, NeuronPermissionType::SubmitProposal)?;

        let min_dissolve_delay_for_vote = nervous_system_parameters
            .neuron_minimum_dissolve_delay_to_vote_seconds
            .expect("NervousSystemParameters must have min_dissolve_delay_for_vote");

        let proposer_dissolve_delay = proposer.dissolve_delay_seconds(now_seconds);
        if proposer_dissolve_delay < min_dissolve_delay_for_vote {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "The proposer's dissolve delay {proposer_dissolve_delay} is less than the minimum required dissolve delay of {min_dissolve_delay_for_vote}"
                ),
            ));
        }

        // If the current stake of the proposer neuron is less than the cost
        // of having a proposal rejected, the neuron cannot make a proposal.
        if proposer.stake_e8s() < reject_cost_e8s {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Neuron doesn't have enough stake to submit proposal.",
            ));
        }

        // Check that there are not too many proposals.  What matters
        // here is the number of proposals for which ballots have not
        // yet been cleared, because ballots take the most amount of
        // space.
        if self
            .proto
            .proposals
            .values()
            .filter(|data| !data.ballots.is_empty())
            .count()
            >= MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS
            && !proposal.allowed_when_resources_are_low()
        {
            return Err(GovernanceError::new_with_message(
                ErrorType::ResourceExhausted,
                "Reached maximum number of proposals that have not yet \
                been taken into account for voting rewards. \
                Please try again later.",
            ));
        }
```

**File:** rs/sns/governance/src/governance.rs (L3600-3637)
```rust

        // Create a new proposal ID for this proposal.
        let proposal_num = self.next_proposal_id();
        let proposal_id = ProposalId { id: proposal_num };

        // Create the proposal.
        let mut proposal_data = ProposalData {
            action: u64::from(action),
            id: Some(proposal_id),
            proposer: Some(proposer_id.clone()),
            reject_cost_e8s,
            proposal: Some(proposal.clone()),
            proposal_creation_timestamp_seconds: now_seconds,
            ballots: electoral_roll,
            payload_text_rendering: Some(rendering),
            initial_voting_period_seconds,
            wait_for_quiet_deadline_increase_seconds,
            // Writing these explicitly so that we have to make a conscious decision
            // about what to do when adding a new field to `ProposalData`.
            latest_tally: ProposalData::default().latest_tally,
            decided_timestamp_seconds: ProposalData::default().decided_timestamp_seconds,
            executed_timestamp_seconds: ProposalData::default().executed_timestamp_seconds,
            failed_timestamp_seconds: ProposalData::default().failed_timestamp_seconds,
            failure_reason: ProposalData::default().failure_reason,
            reward_event_round: ProposalData::default().reward_event_round,
            wait_for_quiet_state: ProposalData::default().wait_for_quiet_state,
            reward_event_end_timestamp_seconds: ProposalData::default()
                .reward_event_end_timestamp_seconds,
            minimum_yes_proportion_of_total: Some(minimum_yes_proportion_of_total),
            minimum_yes_proportion_of_exercised: Some(minimum_yes_proportion_of_exercised),
            // This field is on its way to deletion, but before we can do that, we temporarily
            // set it to true. It used to be that this was set based on whether the reward rate
            // is positive, but that was a mistake. That's why we are getting rid of this.
            // TODO(NNS1-2731): Delete this.
            is_eligible_for_rewards: true,
            action_auxiliary,
            topic: Some(i32::from(proposal_topic)),
        };
```

**File:** rs/sns/governance/src/governance.rs (L3644-3653)
```rust
        // Charge the cost of rejection upfront.
        // This will protect from DoS in couple of ways:
        // - It prevents a neuron from having too many proposals outstanding.
        // - It reduces the voting power of the submitter so that for every proposal
        //   outstanding the submitter will have less voting power to get it approved.
        self.proto
            .neurons
            .get_mut(&proposer_id.to_string())
            .expect("Proposer not found.")
            .neuron_fees_e8s += proposal_data.reject_cost_e8s;
```

**File:** rs/sns/integration_tests/src/initialization_flow.rs (L1258-1264)
```rust
    // Submit a copy of the same proposal. This should succeed since there is no deduping mechanism
    // for SNS content
    let proposal_id = sns_initialization_flow_test.propose_create_service_nervous_system(
        get_neuron_1().principal_id,
        get_neuron_1().neuron_id,
        &proposal,
    );
```
