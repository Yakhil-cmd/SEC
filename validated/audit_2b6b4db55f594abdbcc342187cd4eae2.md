### Title
Critical Governance Parameters (`neuron_minimum_dissolve_delay_to_vote_seconds`, `reject_cost_e8s`) Can Be Changed Mid-Voting-Round, Causing Severe Inconsistencies - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS governance canister allows a `ManageNervousSystemParameters` proposal to be executed while other proposals are still open for voting. This immediately overwrites critical parameters — specifically `neuron_minimum_dissolve_delay_to_vote_seconds` and `reject_cost_e8s` — that were used to construct the electoral roll and cost structure of already-open proposals. The result is a mid-round parameter change that breaks the fairness and consistency guarantees of the voting system, directly analogous to the `commissionPercent`/`baseStake` mid-round mutation in the Revolver.sol report.

---

### Finding Description

When a new SNS proposal is created, `compute_ballots_for_new_proposal` reads `neuron_minimum_dissolve_delay_to_vote_seconds` from the live `NervousSystemParameters` to determine which neurons are eligible to vote and what their voting power is. The resulting electoral roll is frozen into the `ProposalData.ballots` map at creation time. [1](#0-0) 

However, when a `ManageNervousSystemParameters` proposal is adopted and executed, `perform_manage_nervous_system_parameters` immediately overwrites `self.proto.parameters` with the new values — with no check for whether other proposals are currently open for voting: [2](#0-1) 

The execution path is:

```
perform_action(proposal_id, Action::ManageNervousSystemParameters(params))
  → perform_manage_nervous_system_parameters(params)
      → self.proto.parameters = Some(new_params);   // immediate, unconditional
``` [3](#0-2) 

The two most impactful parameters that can be changed mid-round are:

**1. `neuron_minimum_dissolve_delay_to_vote_seconds`**

This is used at proposal creation to decide which neurons receive ballots. If this threshold is *lowered* mid-round via a `ManageNervousSystemParameters` proposal, neurons that were ineligible when existing open proposals were created (and thus received no ballot) remain excluded from those proposals — but newly created proposals will include them. Conversely, if the threshold is *raised*, neurons that already have ballots on open proposals retain their voting power, while the same neurons would be excluded from any new proposal. This creates an inconsistent two-tier electorate across concurrent proposals. [4](#0-3) 

**2. `reject_cost_e8s`**

This is read at proposal creation time to check whether the proposer has sufficient stake. If `reject_cost_e8s` is raised mid-round, proposers who submitted proposals under the old (lower) cost are not retroactively penalized — but new proposers face a higher barrier. If it is lowered, the reverse inconsistency applies. The cost charged on rejection is the value stored in `ProposalData.reject_cost_e8s` at creation time, so the fee structure differs across concurrent proposals. [5](#0-4) 

The `NervousSystemParameters` proto definition confirms both fields are mutable via governance proposal: [6](#0-5) 

The `validate_and_render_manage_nervous_system_parameters` function performs no check for open proposals before allowing the change: [7](#0-6) 

---

### Impact Explanation

- **Governance authorization bug / mid-round parameter inconsistency**: An SNS community can pass a `ManageNervousSystemParameters` proposal that immediately changes `neuron_minimum_dissolve_delay_to_vote_seconds` or `reject_cost_e8s` while other proposals are still open. This creates:
  - **Unequal electoral rolls across concurrent proposals**: Neurons eligible to vote on proposal A (created before the change) may not be eligible on proposal B (created after), or vice versa, even though both are open simultaneously.
  - **Inconsistent proposal costs**: Proposers who submitted before the `reject_cost_e8s` change face different economic consequences than those who submit after, even within the same voting window.
  - **Governance manipulation**: A coordinated actor controlling enough voting power to pass a `ManageNervousSystemParameters` proposal can strategically time the parameter change to exclude or include specific neurons from voting on a concurrent critical proposal (e.g., a `TransferSnsTreasuryFunds` proposal).

---

### Likelihood Explanation

This is reachable by any SNS token holder with sufficient voting power to pass a `ManageNervousSystemParameters` proposal (which is a Critical-class proposal requiring >67% of total voting power). The attack path is:

1. Submit a `TransferSnsTreasuryFunds` or other high-value proposal (Proposal A).
2. While Proposal A is open, submit and pass a `ManageNervousSystemParameters` proposal that lowers `neuron_minimum_dissolve_delay_to_vote_seconds` to include previously ineligible neurons that are controlled by the attacker.
3. Those newly eligible neurons cannot vote on Proposal A (no ballot was issued to them), but the attacker's existing neurons already have ballots. The attacker has effectively changed the effective quorum composition mid-vote.

The entry path is a standard ingress `manage_neuron` call — no privileged access beyond normal SNS token-holder voting power is required.

---

### Recommendation

1. **Defer parameter changes**: In `perform_manage_nervous_system_parameters`, check whether any proposals are currently open (i.e., `proposal.status() == Open`). If so, queue the parameter change to take effect only after all currently open proposals have been decided/settled.
2. **Alternatively, snapshot parameters per proposal**: Store the `neuron_minimum_dissolve_delay_to_vote_seconds` and `reject_cost_e8s` values in `ProposalData` at creation time and use those snapshots for all subsequent operations on that proposal (the `initial_voting_period_seconds` is already snapshotted this way in `ProposalData`). [8](#0-7) 

---

### Proof of Concept

1. SNS is initialized with `neuron_minimum_dissolve_delay_to_vote_seconds = 6 months`. Neurons A (8-month delay) and B (1-month delay) exist. Only A has a ballot on any new proposal.
2. Neuron A submits Proposal 1: `TransferSnsTreasuryFunds` (open, A has ballot, B does not).
3. Neuron A also submits and votes to pass Proposal 2: `ManageNervousSystemParameters { neuron_minimum_dissolve_delay_to_vote_seconds: 2 weeks }`.
4. Proposal 2 executes immediately, updating `self.proto.parameters`. [9](#0-8) 

5. Proposal 1 is still open. Neuron B (1-month delay, now above the new 2-week threshold) has **no ballot** on Proposal 1 — it was excluded at creation time. Neuron A retains its ballot and can decide Proposal 1 unilaterally, even though under the new rules B should be eligible.
6. Conversely, if the threshold is raised (e.g., to 1 year), neurons with 6-month delays that already have ballots on open proposals retain their voting power, while the same neurons are excluded from any new proposal — creating a two-tier electorate within the same voting window.

### Citations

**File:** rs/sns/governance/src/governance.rs (L2139-2146)
```rust
    async fn perform_action(&mut self, proposal_id: u64, action: Action) {
        let result = match action {
            // Execution of Motion proposals is trivial.
            Action::Motion(_) => Ok(()),

            Action::ManageNervousSystemParameters(params) => {
                self.perform_manage_nervous_system_parameters(params)
            }
```

**File:** rs/sns/governance/src/governance.rs (L2581-2617)
```rust
    fn perform_manage_nervous_system_parameters(
        &mut self,
        proposed_params: NervousSystemParameters,
    ) -> Result<(), GovernanceError> {
        // Only set `self.proto.parameters` if "applying" the proposed params to the
        // current params results in valid params
        let new_params = proposed_params.inherit_from(self.nervous_system_parameters_or_panic());

        log!(
            INFO,
            "Setting Governance nervous system params to: {:?}",
            &new_params
        );

        match new_params.validate() {
            Ok(()) => {
                self.proto.parameters = Some(new_params);
                Ok(())
            }

            // Even though proposals are validated when they are first made, this is still
            // possible, because the inner value of a ManageNervousSystemParameters
            // proposal is only valid with respect to the current
            // nervous_system_parameters() at the time when the proposal was first
            // made. If nervous_system_parameters() changed (by another proposal) since
            // the current proposal was first made, the current proposal might have become
            // invalid. Basically, this might occur if there are conflicting (concurrent)
            // proposals, but we expect this to be highly unusual in practice.
            Err(msg) => Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "Failed to perform ManageNervousSystemParameters action, proposed \
                        parameters would lead to invalid NervousSystemParameters: {msg}"
                ),
            )),
        }
    }
```

**File:** rs/sns/governance/src/governance.rs (L3489-3526)
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
```

**File:** rs/sns/governance/src/governance.rs (L3606-3637)
```rust
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

**File:** rs/sns/governance/src/governance.rs (L5248-5261)
```rust
        let min_dissolve_delay_for_vote = nervous_system_parameters
            .neuron_minimum_dissolve_delay_to_vote_seconds
            .expect("NervousSystemParameters must have min_dissolve_delay_for_vote");

        let mut electoral_roll = BTreeMap::<String, Ballot>::new();
        let mut total_power: u128 = 0;

        for (k, v) in self.proto.neurons.iter() {
            // If this neuron is eligible to vote, record its
            // voting power at the time of proposal creation (now).
            if v.dissolve_delay_seconds(now_seconds) < min_dissolve_delay_for_vote {
                // Not eligible due to dissolve delay.
                continue;
            }
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1118-1132)
```text
message NervousSystemParameters {
  // The number of e8s (10e-8 of a token) that a rejected
  // proposal costs the proposer.
  optional uint64 reject_cost_e8s = 1;

  // The minimum number of e8s (10e-8 of a token) that can be staked in a neuron.
  //
  // To ensure that staking and disbursing of the neuron work, the chosen value
  // must be larger than the transaction_fee_e8s.
  optional uint64 neuron_minimum_stake_e8s = 2;

  // The transaction fee that must be paid for ledger transactions (except
  // minting and burning governance tokens).
  optional uint64 transaction_fee_e8s = 3;

```

**File:** rs/sns/governance/src/proposal.rs (L527-549)
```rust
/// Validates and renders a proposal with action ManageNervousSystemParameters.
fn validate_and_render_manage_nervous_system_parameters(
    new_parameters: &NervousSystemParameters,
    current_parameters: &NervousSystemParameters,
) -> Result<String, String> {
    if new_parameters == &NervousSystemParameters::default() {
        return Err("NervousSystemParameters: at least one field must be set.".to_string());
    }

    new_parameters.inherit_from(current_parameters).validate()?;

    Ok(format!(
        r"# Proposal to change nervous system parameters:
## Current nervous system parameters:

{:#?}

## New nervous system parameters:

{:#?}",
        &current_parameters, new_parameters
    ))
}
```
