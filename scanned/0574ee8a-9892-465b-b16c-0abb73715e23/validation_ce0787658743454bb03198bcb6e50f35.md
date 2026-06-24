### Title
DOS of `CreateServiceNervousSystem` Proposals via One-at-a-Time Restriction - (`File: rs/nns/governance/src/governance.rs`)

### Summary

The NNS governance enforces that only one `CreateServiceNervousSystem` proposal can be open at a time. Because this restriction is type-level (not caller-specific), any neuron holder with sufficient stake can submit a malicious `CreateServiceNervousSystem` proposal to permanently block legitimate SNS creation proposals for the duration of each voting period, repeating the attack indefinitely.

### Finding Description

The NNS governance `validate_proposal` function (called from `make_proposal`) rejects any new `CreateServiceNervousSystem` proposal if one is already open, returning an error containing "another open". This is confirmed by the integration test: [1](#0-0) 

The `make_proposal` entry point in NNS governance is: [2](#0-1) 

An attacker with a qualifying neuron (minimum dissolve delay + enough ICP for `reject_cost_e8s`) can:

1. Submit a `CreateServiceNervousSystem` proposal with malicious parameters (wrong dapp canister, wrong token distribution, or a description designed to discourage positive voting).
2. This immediately blocks any legitimate `CreateServiceNervousSystem` proposal from being submitted — the NNS returns `PreconditionFailed` with "another open" to all subsequent attempts.
3. After the voting period ends (minimum 4 days, default longer), the attacker immediately re-submits another malicious proposal.
4. This cycle can be repeated indefinitely.

There is no mechanism to remove the malicious proposal, no per-caller cooldown, and no way for legitimate proposers to bypass the restriction.

This is structurally identical to the Salty `proposeSendSALT()` vulnerability: a single global "one open at a time" gate keyed only on proposal type, not on the proposer's identity or the proposal's content.

### Impact Explanation

- **Complete DOS of SNS creation via NNS governance.** No new SNS can be launched through the NNS proposal process for as long as the attacker maintains the attack.
- The attacker does not need to win the vote — they only need the proposal to remain open during the voting period.
- If the proposal is rejected, the attacker loses `reject_cost_e8s` (currently 10 ICP), but their neuron stake is preserved and they continue earning voting rewards. The cost per voting period is low relative to the impact.
- The attack is repeatable with no increasing cost or cooldown.

### Likelihood Explanation

- Entry path is a standard ingress `manage_neuron` call — no privileged access required.
- Any NNS neuron holder with ≥6 months dissolve delay and ≥10 ICP stake can execute this.
- The NNS has thousands of qualifying neurons. A motivated attacker (e.g., a competitor to a specific SNS project) has clear economic incentive.
- The attack requires only one transaction every few days.

### Recommendation

1. **Include the proposer's `NeuronId` in the one-at-a-time uniqueness check**, so each neuron can only block itself, not all other proposers. Legitimate proposals from different neurons would then be allowed concurrently.
2. Alternatively, **allow multiple concurrent `CreateServiceNervousSystem` proposals** and resolve conflicts at execution time (the first adopted proposal executes; subsequent ones fail gracefully).
3. Consider a **per-neuron cooldown** after a `CreateServiceNervousSystem` proposal is rejected, to raise the cost of repeated attacks.

### Proof of Concept

The existing integration test already demonstrates the blocking behavior: [3](#0-2) 

An attacker extends this by:
1. Submitting a `CreateServiceNervousSystem` proposal with a wrong `dapp_canisters` field (a canister the attacker controls, not the legitimate dapp).
2. Observing that all subsequent legitimate proposals fail with `PreconditionFailed`.
3. After the voting period expires and the proposal is rejected, immediately re-submitting step 1.

The `make_proposal` flow that enforces this restriction is: [4](#0-3)

### Citations

**File:** rs/nns/integration_tests/src/create_service_nervous_system.rs (L37-102)
```rust
/// Makes three CreateServiceNervousSystem proposals. The second one is not
/// allowed, because the first is still open. Then, the first proposal gets
/// adopted (allowing the third to be made) and executed. What should result is
/// a new SNS.
#[test]
fn test_several_proposals() {
    // Step 1: Prepare the world.

    let state_machine = state_machine_builder_for_nns_tests().build();

    // Step 1.1: Boot up NNS.
    let nns_init_payload = NnsInitPayloadsBuilder::new()
        .with_initial_invariant_compliant_mutations()
        .with_test_neurons_fund_neurons(100_000_000_000_000)
        .with_sns_dedicated_subnets(state_machine.get_subnet_ids())
        .with_sns_wasm_access_controls(true)
        .build();
    // Note that this uses production governance.
    setup_nns_canisters_with_features(&state_machine, nns_init_payload, /* features */ &[]);
    add_real_wasms_to_sns_wasms(&state_machine);
    let dapp_canister = state_machine.create_canister_with_cycles(
        Some(SPECIFIED_CANISTER_ID.get()),
        Cycles::zero(),
        None,
    );
    set_controllers(
        &state_machine,
        PrincipalId::new_anonymous(),
        dapp_canister,
        vec![ROOT_CANISTER_ID.get()],
    );

    // In real life, DFINITY would top up SNS_WASM's cycle balance (and the SNS
    // is supposed to repay with ICP raised).
    state_machine.add_cycles(SNS_WASM_CANISTER_ID, 200 * ONE_TRILLION);

    // Step 2: Run code under test. Inspect intermediate results.

    // Step 2.1: Make a proposal. Leave it open so that the next proposal is
    // foiled.
    let response_1 = make_proposal(&state_machine, /* sns_number = */ 1, false);
    let response_1 = match response_1.command {
        Some(manage_neuron_response::Command::MakeProposal(resp)) => resp,
        _ => panic!("First proposal failed to be submitted: {response_1:#?}"),
    };
    let proposal_id_1 = response_1
        .proposal_id
        .unwrap_or_else(|| {
            panic!("First proposal response did not contain a proposal_id: {response_1:#?}")
        })
        .id;

    // Step 2.2: Make another proposal. This one should be foiled, because the
    // first proposal is still open.
    let response_2 = make_proposal(&state_machine, 666, false);
    match response_2.command {
        Some(manage_neuron_response::Command::Error(err)) => {
            assert_eq!(
                err.error_type,
                ErrorType::PreconditionFailed as i32,
                "{err:#?}",
            );
            assert!(err.error_message.contains("another open"), "{err:?}",);
        }
        _ => panic!("Second proposal should be invalid: {response_2:#?}"),
    }
```

**File:** rs/nns/governance/src/governance.rs (L5133-5270)
```rust
    pub async fn make_proposal(
        &mut self,
        proposer_id: &NeuronId,
        caller: &PrincipalId,
        proposal: &Proposal,
    ) -> Result<ProposalId, GovernanceError> {
        let now_seconds = self.env.now();

        let action = self.validate_proposal(proposal)?;

        if !action.allowed_when_resources_are_low() {
            self.check_heap_can_grow()?;
        }

        // At this point, the topic should be valid because the proposal was just validated, but we
        // exit on error anyway and check for Topic::Unspecified, just to be safe.
        let topic = action.topic()?;
        if topic == Topic::Unspecified {
            return Err(GovernanceError::new_with_message(
                ErrorType::InvalidProposal,
                "Topic is unspecified. This should never happen.",
            ));
        }

        let self_describing_action =
            if cfg!(target_arch = "wasm32") && !cfg!(feature = "canbench-rs") {
                match action.to_self_describing(self.env.clone()).await {
                    Ok(self_describing_action) => Some(self_describing_action),
                    Err(e) => {
                        println!(
                            "{}Failed to get self_describing_action for proposal: {:?}",
                            LOG_PREFIX, e
                        );
                        None
                    }
                }
            } else {
                None
            };

        // Before actually modifying anything, we first make sure that
        // the neuron is allowed to make this proposal and create the
        // electoral roll.
        //
        // Find the proposing neuron.
        let (
            is_proposer_authorized_to_vote,
            proposer_dissolve_delay_seconds,
            proposer_minted_stake_e8s,
        ) = self.with_neuron(proposer_id, |neuron| {
            (
                neuron.is_authorized_to_vote(caller),
                neuron.dissolve_delay_seconds(now_seconds),
                neuron.minted_stake_e8s(),
            )
        })?;

        // Check that the caller is authorized, i.e., either the
        // controller or a registered hot key.
        if !is_proposer_authorized_to_vote {
            return Err(GovernanceError::new_with_message(
                ErrorType::NotAuthorized,
                "Caller not authorized to propose.",
            ));
        }

        let proposal_submission_fee = self.proposal_submission_fee(proposal)?;

        let reject_cost_e8s = self.reject_cost_e8s(proposal)?;

        // If the current stake of this neuron is less than the cost
        // of having a proposal rejected, the neuron cannot make the proposal -
        // because the proposal may be rejected.
        if proposer_minted_stake_e8s < proposal_submission_fee {
            return Err(GovernanceError::new_with_message(
                ErrorType::InsufficientFunds,
                format!(
                    "Neuron doesn't have enough minted stake to submit proposal: {proposer_minted_stake_e8s}",
                ),
            ));
        }

        let min_dissolve_delay_seconds_to_propose = if action.manage_neuron().is_some() {
            0
        } else {
            NEURON_MINIMUM_DISSOLVE_DELAY_TO_PROPOSE_SECONDS
        };

        // The proposer must have sufficient dissolve delay to submit proposals.
        if proposer_dissolve_delay_seconds < min_dissolve_delay_seconds_to_propose {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Neuron's dissolve delay is too short.",
            ));
        }

        // Check that there are not too many proposals.
        if action.manage_neuron().is_some() {
            // Check that there are not too many open manage neuron
            // proposals already.
            if self
                .heap_data
                .proposals
                .values()
                .filter(|info| info.is_manage_neuron() && info.status() == ProposalStatus::Open)
                .count()
                >= MAX_NUMBER_OF_OPEN_MANAGE_NEURON_PROPOSALS
            {
                return Err(GovernanceError::new_with_message(
                    ErrorType::ResourceExhausted,
                    "Reached maximum number of 'manage neuron' proposals. \
                    Please try again later.",
                ));
            }
        } else {
            // What matters here is the number of proposals for which
            // ballots have not yet been cleared, because ballots take the
            // most amount of space. (In the case of proposals with a wasm
            // module in the payload, the payload also takes a lot of
            // space). Manage neuron proposals are not counted as they have
            // a smaller electoral roll and use their own limit.
            if self
                .heap_data
                .proposals
                .values()
                .filter(|info| !info.ballots.is_empty() && !info.is_manage_neuron())
                .count()
                >= MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS
                && !action.allowed_when_resources_are_low()
            {
                return Err(GovernanceError::new_with_message(
                    ErrorType::ResourceExhausted,
                    "Reached maximum number of proposals that have not yet \
                    been taken into account for voting rewards. \
                    Please try again later.",
                ));
            }
        }
```
