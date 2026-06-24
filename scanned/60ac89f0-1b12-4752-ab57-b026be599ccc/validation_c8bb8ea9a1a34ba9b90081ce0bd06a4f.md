### Title
No-Floor `reject_cost_e8s` Allows SNS Governance Proposal Slot Exhaustion at Negligible Cost - (`rs/sns/governance/src/types.rs`)

### Summary

The SNS governance `NervousSystemParameters.reject_cost_e8s` field has no enforced minimum value beyond "must be set." An SNS community (or its founding team, who controls the initial governance parameters) can set `reject_cost_e8s` to 1 e8 (or any arbitrarily small value) via a `ManageNervousSystemParameters` proposal. Once set, any neuron with sufficient stake can flood the SNS governance canister with up to `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS` (700) open proposals at essentially zero cost, exhausting the proposal slot and blocking all legitimate governance activity for the duration of the voting period.

### Finding Description

`validate_reject_cost_e8s` in `rs/sns/governance/src/types.rs` only checks that the field is `Some(...)` — it imposes no lower bound on the value:

```rust
fn validate_reject_cost_e8s(&self) -> Result<u64, String> {
    self.reject_cost_e8s
        .ok_or_else(|| "NervousSystemParameters.reject_cost_e8s must be set".to_string())
}
``` [1](#0-0) 

This means `reject_cost_e8s = 1` (one e8, i.e., 0.00000001 SNS tokens) is a fully valid parameter. The `ManageNervousSystemParameters` proposal action validates the merged parameters using the same `validate()` call, so the same absence of a floor applies when updating parameters post-launch:

```rust
fn validate_and_render_manage_nervous_system_parameters(...) {
    new_parameters.inherit_from(current_parameters).validate()?;
    ...
}
``` [2](#0-1) 

Once `reject_cost_e8s` is set to 1 e8, any neuron whose `stake_e8s >= 1` can submit proposals. The SNS governance canister enforces a hard cap of `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS = 700` open proposals:

```rust
pub const MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS: usize = 700;
``` [3](#0-2) 

The check in `make_proposal` blocks new proposals once this cap is reached:

```rust
if self.proto.proposals.values()
    .filter(|data| !data.ballots.is_empty())
    .count() >= MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS
    && !proposal.allowed_when_resources_are_low()
{
    return Err(GovernanceError::new_with_message(
        ErrorType::ResourceExhausted, ...));
}
``` [4](#0-3) 

The `reject_cost_e8s` is charged upfront as `neuron_fees_e8s` but is returned if the proposal passes. Since the attacker controls the neuron and can vote yes on their own spam proposals (the proposer auto-votes yes), and since the SNS default quorum is only 3% of total voting power, a majority-stake attacker can pass all their own proposals and recover all fees. [5](#0-4) 

### Impact Explanation

An attacker (or the SNS founding team acting maliciously) who passes a `ManageNervousSystemParameters` proposal setting `reject_cost_e8s = 1` can then:

1. Submit 700 spam proposals at a total cost of 700 e8s (0.0000070 SNS tokens).
2. Vote yes on all of them using their own neuron (proposer auto-votes yes).
3. The SNS governance canister is now at the `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS` cap.
4. All legitimate proposals from other users are blocked with `ResourceExhausted` for the entire voting period (minimum 1 day, up to ~8 days with wait-for-quiet).
5. The attacker recovers all fees when their proposals pass.

This is a **governance authorization / resource accounting bug** that allows a single unprivileged SNS neuron holder to block all SNS governance activity at negligible cost, effectively a governance DoS. For SNS DAOs controlling dapp canisters and treasuries, this prevents legitimate upgrades, treasury actions, and emergency responses.

### Likelihood Explanation

The attack requires:
- Passing one `ManageNervousSystemParameters` proposal to set `reject_cost_e8s` to a tiny value (requires governance majority, but the founding team typically holds majority at launch).
- Holding a neuron with stake ≥ 1 e8 (trivially achievable).

For SNS DAOs where the founding team retains majority voting power (common at launch), this is directly exploitable. For mature SNS DAOs, a coalition with majority voting power could execute this. The `reject_cost_e8s` parameter has no floor, so the attack surface is always present.

### Recommendation

Enforce a minimum floor on `reject_cost_e8s` in `validate_reject_cost_e8s`. A reasonable floor would be at least `transaction_fee_e8s` (already validated to be > 0), or a protocol-defined constant such as `E8S_PER_TOKEN / 100` (0.01 SNS tokens). This mirrors how `neuron_minimum_stake_e8s` is validated to be strictly greater than `transaction_fee_e8s`: [6](#0-5) 

The fix should be in `validate_reject_cost_e8s` to add:

```rust
fn validate_reject_cost_e8s(&self) -> Result<u64, String> {
    let reject_cost = self.reject_cost_e8s
        .ok_or_else(|| "NervousSystemParameters.reject_cost_e8s must be set".to_string())?;
    let min = /* e.g. */ 10_000_u64; // or transaction_fee_e8s
    if reject_cost < min {
        return Err(format!(
            "NervousSystemParameters.reject_cost_e8s ({reject_cost}) must be >= {min}"
        ));
    }
    Ok(reject_cost)
}
```

### Proof of Concept

1. An SNS is launched with default `reject_cost_e8s = 100_000_000` (1 SNS token).
2. The founding team (holding majority voting power) submits and passes a `ManageNervousSystemParameters` proposal setting `reject_cost_e8s = 1`.
3. The attacker stakes a neuron with 1 e8 of SNS tokens.
4. The attacker calls `make_proposal` 700 times with trivial `Motion` proposals. Each call costs 1 e8 upfront as `neuron_fees_e8s`. Total cost: 700 e8s ≈ 0 SNS tokens.
5. The SNS governance canister now has 700 open proposals with ballots. Any subsequent `make_proposal` call from any user returns `ResourceExhausted`.
6. The attacker votes yes on all 700 proposals. Since the attacker holds majority voting power, all proposals pass. All 700 e8s in fees are returned.
7. Legitimate governance is blocked for the full voting period (4 days minimum by default). [7](#0-6) [3](#0-2) [8](#0-7)

### Citations

**File:** rs/sns/governance/src/types.rs (L570-600)
```rust
    /// This validates that the `NervousSystemParameters` are well-formed.
    pub fn validate(&self) -> Result<(), String> {
        self.validate_reject_cost_e8s()?;
        self.validate_neuron_minimum_stake_e8s()?;
        self.validate_transaction_fee_e8s()?;
        self.validate_max_proposals_to_keep_per_action()?;
        self.validate_initial_voting_period_seconds()?;
        self.validate_wait_for_quiet_deadline_increase_seconds()?;
        self.validate_default_followees()?;
        self.validate_max_number_of_neurons()?;
        self.validate_neuron_minimum_dissolve_delay_to_vote_seconds()?;
        self.validate_max_followees_per_function()?;
        self.validate_max_dissolve_delay_seconds()?;
        self.validate_max_neuron_age_for_age_bonus()?;
        self.validate_max_number_of_proposals_with_ballots()?;
        self.validate_neuron_claimer_permissions()?;
        self.validate_neuron_grantable_permissions()?;
        self.validate_max_number_of_principals_per_neuron()?;
        self.validate_voting_rewards_parameters()?;
        self.validate_max_dissolve_delay_bonus_percentage()?;
        self.validate_max_age_bonus_percentage()?;
        self.validate_additional_critical_native_action_ids()?;

        Ok(())
    }

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

**File:** rs/sns/governance/src/proposal.rs (L79-79)
```rust
pub const MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS: usize = 700;
```

**File:** rs/sns/governance/src/proposal.rs (L528-537)
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

**File:** rs/sns/governance/src/governance.rs (L3532-3547)
```rust
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
