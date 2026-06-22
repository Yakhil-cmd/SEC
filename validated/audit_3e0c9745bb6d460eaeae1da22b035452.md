### Title
SNS Governance `reject_cost_e8s = 0` Bypasses Proposal Stake Requirement, Enabling Permissionless Governance DoS — (`rs/sns/governance/src/types.rs`)

---

### Summary

`NervousSystemParameters.validate_reject_cost_e8s` only checks that the field is `Some(...)`, not that it is greater than zero. When an SNS community sets `reject_cost_e8s` to `0` via a `ManageNervousSystemParameters` proposal, the stake guard in `make_proposal` is trivially bypassed for every neuron, allowing any neuron holder — regardless of stake — to submit unlimited proposals at zero economic cost, filling the governance queue and blocking legitimate proposals.

---

### Finding Description

**Root cause — missing lower-bound in validation:**

In `rs/sns/governance/src/types.rs`, `validate_reject_cost_e8s` only asserts presence:

```rust
fn validate_reject_cost_e8s(&self) -> Result<u64, String> {
    self.reject_cost_e8s
        .ok_or_else(|| "NervousSystemParameters.reject_cost_e8s must be set".to_string())
}
```

`Some(0)` passes this check without error. [1](#0-0) 

The same function is the sole validator called during both proposal submission (`validate_and_render_manage_nervous_system_parameters`) and execution (`perform_manage_nervous_system_parameters`): [2](#0-1) [3](#0-2) 

**Stake guard bypass in `make_proposal`:**

```rust
if proposer.stake_e8s() < reject_cost_e8s {
    return Err(GovernanceError::new_with_message(
        ErrorType::PreconditionFailed,
        "Neuron doesn't have enough stake to submit proposal.",
    ));
}
```

When `reject_cost_e8s == 0`, the condition `stake_e8s() < 0` is always `false` for any `u64` stake value, including zero. The guard is completely neutralised. [4](#0-3) 

**Zero economic cost per proposal:**

```rust
self.proto.neurons.get_mut(&proposer_id.to_string())
    .expect("Proposer not found.")
    .neuron_fees_e8s += proposal_data.reject_cost_e8s;   // += 0
```

No fees are charged to the proposer's neuron, so there is no on-chain economic deterrent to repeated submission. [5](#0-4) 

**Exploit flow:**

1. An SNS community passes a `ManageNervousSystemParameters` proposal with `reject_cost_e8s: Some(0)`. The proposal passes `validate_and_render_manage_nervous_system_parameters` because `validate_reject_cost_e8s` accepts `Some(0)`.
2. After execution, any principal that holds a neuron with `SubmitProposal` permission and the (possibly also-zeroed) minimum dissolve delay can call `make_proposal` in a tight loop.
3. Each call succeeds: the stake check passes (`0 < 0 == false`), no fees are deducted, and a new `ProposalData` entry is created.
4. The governance canister's `max_number_of_proposals_with_ballots` limit is reached, after which new proposals are rejected with `ResourceExhausted` — blocking all legitimate governance actions until the queue drains.

---

### Impact Explanation

- **Governance DoS:** The attacker fills `max_number_of_proposals_with_ballots` (default 700) with worthless proposals. Legitimate proposals — including SNS upgrades, parameter changes, and treasury actions — cannot be submitted until the queue drains, which requires the voting period to expire for every queued proposal.
- **Voting-power dilution:** Each spam proposal forces all neurons to cast ballots (or follow), consuming governance bandwidth and potentially diluting reward-eligible voting power.
- **No economic recovery:** Because `neuron_fees_e8s` is incremented by 0, the attacker's neuron accumulates no fees and suffers no penalty even if every proposal is rejected.

---

### Likelihood Explanation

An SNS community might deliberately set `reject_cost_e8s` to 0 to lower the barrier to participation (analogous to the Morpho curator setting `forceDeallocatePenalty` to 0 to allow "pinging"). The code provides no warning, no floor enforcement, and no documentation that zero is unsafe. Once set, any neuron holder — not just the governance majority that approved the change — can exploit the open gate. The attacker needs only a neuron with `SubmitProposal` permission, which is the default claimer permission. [6](#0-5) 

---

### Recommendation

Enforce a non-zero minimum in `validate_reject_cost_e8s`:

```rust
fn validate_reject_cost_e8s(&self) -> Result<u64, String> {
    let cost = self.reject_cost_e8s
        .ok_or_else(|| "NervousSystemParameters.reject_cost_e8s must be set".to_string())?;
    if cost == 0 {
        return Err(
            "NervousSystemParameters.reject_cost_e8s must be greater than zero \
             to prevent permissionless proposal spam".to_string()
        );
    }
    Ok(cost)
}
```

Alternatively, enforce `reject_cost_e8s >= transaction_fee_e8s` (analogous to the existing `neuron_minimum_stake_e8s > transaction_fee_e8s` guard) so that every rejected proposal burns at least one ledger fee. [7](#0-6) 

---

### Proof of Concept

```
// Step 1 – governance passes ManageNervousSystemParameters
NervousSystemParameters { reject_cost_e8s: Some(0), ..Default::default() }
// validate_reject_cost_e8s returns Ok(0)  ← no floor check

// Step 2 – attacker (any neuron holder) calls make_proposal in a loop
for _ in 0..700 {
    governance.make_proposal(
        &attacker_neuron_id,
        &attacker_principal,
        &Proposal { action: Some(Action::Motion(Motion { motion_text: "".into() })), .. }
    ).await.unwrap();
    // stake check: attacker_stake (e.g. 1 e8s) < 0  → false → passes
    // neuron_fees_e8s += 0  → no cost
}

// Step 3 – legitimate proposal is rejected
governance.make_proposal(&legitimate_neuron, &legitimate_principal, &real_proposal)
    .await
    .unwrap_err();
// GovernanceError { error_type: ResourceExhausted,
//   message: "Reached maximum number of proposals that have not yet been taken
//             into account for voting rewards." }
``` [8](#0-7) [9](#0-8)

### Citations

**File:** rs/sns/governance/src/types.rs (L469-493)
```rust
    pub fn with_default_values() -> Self {
        Self {
            reject_cost_e8s: Some(E8S_PER_TOKEN), // 1 governance token
            neuron_minimum_stake_e8s: Some(E8S_PER_TOKEN), // 1 governance token
            transaction_fee_e8s: Some(DEFAULT_TRANSFER_FEE.get_e8s()),
            max_proposals_to_keep_per_action: Some(100),
            initial_voting_period_seconds: Some(4 * ONE_DAY_SECONDS), // 4d
            wait_for_quiet_deadline_increase_seconds: Some(ONE_DAY_SECONDS), // 1d
            default_followees: Some(DefaultFollowees::default()),
            max_number_of_neurons: Some(200_000),
            neuron_minimum_dissolve_delay_to_vote_seconds: Some(6 * ONE_MONTH_SECONDS), // 6m
            max_followees_per_function: Some(15),
            max_dissolve_delay_seconds: Some(8 * ONE_YEAR_SECONDS), // 8y
            max_neuron_age_for_age_bonus: Some(4 * ONE_YEAR_SECONDS), // 4y
            max_number_of_proposals_with_ballots: Some(700),
            neuron_claimer_permissions: Some(Self::default_neuron_claimer_permissions()),
            neuron_grantable_permissions: Some(NeuronPermissionList::default()),
            max_number_of_principals_per_neuron: Some(5),
            voting_rewards_parameters: Some(VotingRewardsParameters::with_default_values()),
            max_dissolve_delay_bonus_percentage: Some(100),
            max_age_bonus_percentage: Some(25),
            maturity_modulation_disabled: Some(false),
            automatically_advance_target_version: Some(true),
            custom_proposal_criticality: None,
        }
```

**File:** rs/sns/governance/src/types.rs (L596-600)
```rust
    /// Validates that the nervous system parameter reject_cost_e8s is well-formed.
    fn validate_reject_cost_e8s(&self) -> Result<u64, String> {
        self.reject_cost_e8s
            .ok_or_else(|| "NervousSystemParameters.reject_cost_e8s must be set".to_string())
    }
```

**File:** rs/sns/governance/src/types.rs (L602-618)
```rust
    /// Validates that the nervous system parameter neuron_minimum_stake_e8s is well-formed.
    fn validate_neuron_minimum_stake_e8s(&self) -> Result<(), String> {
        let transaction_fee_e8s = self.validate_transaction_fee_e8s()?;

        let neuron_minimum_stake_e8s = self.neuron_minimum_stake_e8s.ok_or_else(|| {
            "NervousSystemParameters.neuron_minimum_stake_e8s must be set".to_string()
        })?;

        if neuron_minimum_stake_e8s <= transaction_fee_e8s {
            Err(format!(
                "NervousSystemParameters.neuron_minimum_stake_e8s ({neuron_minimum_stake_e8s}) must be greater than \
                NervousSystemParameters.transaction_fee_e8s ({neuron_minimum_stake_e8s})"
            ))
        } else {
            Ok(())
        }
    }
```

**File:** rs/sns/governance/src/proposal.rs (L528-536)
```rust
fn validate_and_render_manage_nervous_system_parameters(
    new_parameters: &NervousSystemParameters,
    current_parameters: &NervousSystemParameters,
) -> Result<String, String> {
    if new_parameters == &NervousSystemParameters::default() {
        return Err("NervousSystemParameters: at least one field must be set.".to_string());
    }

    new_parameters.inherit_from(current_parameters).validate()?;
```

**File:** rs/sns/governance/src/governance.rs (L2581-2616)
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
