### Title
SNS Governance `ManageNervousSystemParameters` Applies Parameter Changes Instantaneously, Trapping Existing Neuron Holders - (File: `rs/sns/governance/src/governance.rs`)

### Summary
The SNS governance canister's `perform_manage_nervous_system_parameters` function applies changes to `NervousSystemParameters` atomically and immediately upon proposal execution, with no delay or grace period. A malicious or captured SNS governance majority can raise `neuron_minimum_stake_e8s` to a value above existing neurons' stakes, or raise `reject_cost_e8s` above existing neurons' stakes, instantly preventing those neurons from disbursing (via `split_neuron`) or submitting proposals. Existing stakers have no time to react and exit before the parameter change takes effect.

### Finding Description

The `ManageNervousSystemParameters` proposal action is executed by `perform_manage_nervous_system_parameters` in `rs/sns/governance/src/governance.rs`. Upon adoption, it immediately overwrites `self.proto.parameters` with the new values:

```rust
Ok(()) => {
    self.proto.parameters = Some(new_params);
    Ok(())
}
```

The parameters that can be changed include `neuron_minimum_stake_e8s`, `reject_cost_e8s`, `neuron_minimum_dissolve_delay_to_vote_seconds`, and `max_dissolve_delay_seconds`. These parameters are checked at the time of each user operation (split, disburse, propose), not at the time the neuron was created.

Specifically:

1. **`neuron_minimum_stake_e8s` raised above existing stakes**: `split_neuron` checks `parent_neuron.stake_e8s() < min_stake + split.amount_e8s` using the live parameter. If `neuron_minimum_stake_e8s` is raised above a neuron's current stake, the neuron can no longer split, and the user is trapped.

2. **`reject_cost_e8s` raised above existing stakes**: `make_proposal` checks `proposer.stake_e8s() < reject_cost_e8s`. If raised above a neuron's stake, the neuron can no longer submit proposals, including proposals to reverse the change.

3. **`neuron_minimum_dissolve_delay_to_vote_seconds` raised**: Neurons with dissolve delays below the new threshold immediately lose voting eligibility, preventing them from voting on any reversal proposal.

The proto comment itself acknowledges the instantaneous nature: *"Note that a change of a parameter will only affect future actions where this parameter is relevant."* — but provides no protection for existing neurons whose state was valid under the old parameters.

### Impact Explanation

A malicious SNS governance majority (which is a realistic attacker in a newly launched SNS with concentrated token distribution) can:

- Raise `neuron_minimum_stake_e8s` to a value larger than most existing neurons' stakes, making `split_neuron` permanently fail for those neurons. Since `disburse_neuron` in the SNS does not check `neuron_minimum_stake_e8s` directly, users can still disburse fully dissolved neurons — but non-dissolved neurons with dissolve delays cannot exit without dissolving first (which takes time), during which the attacker can further manipulate parameters.
- Raise `reject_cost_e8s` to a value above most neurons' stakes, silencing all minority neuron holders from submitting counter-proposals.
- Raise `neuron_minimum_dissolve_delay_to_vote_seconds` to the maximum allowed value (6 months), instantly disenfranchising all neurons with shorter dissolve delays from voting on any reversal.

The combination of these three changes applied simultaneously can permanently trap token holders: they cannot vote, cannot propose, and cannot split their neurons to exit. Their tokens remain locked in the SNS governance canister for the duration of their dissolve delay.

### Likelihood Explanation

The SNS framework is explicitly designed to allow any project to create an SNS. Early-stage SNSes frequently have concentrated token distributions (developer neurons hold majority voting power). A malicious developer team that retains majority voting power post-swap can pass a `ManageNervousSystemParameters` proposal with a single transaction. The proposal voting period (minimum 1 day) gives users some warning, but the wait-for-quiet algorithm only extends the period if there is a flip in majority — a supermajority attacker can pass the proposal without triggering wait-for-quiet extension. The attack is realistic and has a clear financial motive (trapping tokens to prevent sell pressure or governance opposition).

### Recommendation

1. **Short term**: Document clearly that `ManageNervousSystemParameters` changes take effect immediately and that existing neurons are not grandfathered. Warn SNS token holders to monitor governance proposals.

2. **Long term**: Implement a mandatory time-lock (e.g., one full voting period) between proposal execution and parameter activation for parameters that affect existing neuron operations (`neuron_minimum_stake_e8s`, `reject_cost_e8s`, `neuron_minimum_dissolve_delay_to_vote_seconds`). Alternatively, validate proposed parameter changes against the current neuron population and reject proposals that would immediately invalidate a significant fraction of existing neurons.

### Proof of Concept

**Entry path**: Any SNS governance token holder with majority voting power submits a `ManageNervousSystemParameters` proposal via `manage_neuron` → `MakeProposal` → `Action::ManageNervousSystemParameters`.

**Execution path**:

1. Proposal is adopted (majority votes yes, or wait-for-quiet does not trigger).
2. `start_proposal_execution` → `perform_action` → `perform_manage_nervous_system_parameters` is called.
3. `perform_manage_nervous_system_parameters` calls `proposed_params.inherit_from(current_params)`, validates, then sets `self.proto.parameters = Some(new_params)` — atomically and immediately.
4. All subsequent calls to `split_neuron`, `make_proposal`, or `disburse_neuron` by minority holders now use the new parameters.

**Trap scenario**:
- Attacker sets `neuron_minimum_stake_e8s = u64::MAX / 2` (within the allowed ceiling `MAX_NEURON_MINIMUM_STAKE_E8S_CEILING` if it exists, or any value above existing stakes).
- Victim's neuron has stake = 100_000_000 e8s. After the proposal executes, `split_neuron` checks `parent_neuron.stake_e8s() < min_stake + split.amount_e8s` — since `min_stake` is now enormous, this always fails.
- Simultaneously, `reject_cost_e8s` is raised above the victim's stake, so `make_proposal` also fails.
- The victim's tokens are locked until their dissolve delay expires, during which the attacker controls the SNS. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** rs/sns/governance/src/governance.rs (L1318-1347)
```rust
        if split.amount_e8s < min_stake + transaction_fee_e8s {
            return Err(GovernanceError::new_with_message(
                ErrorType::InsufficientFunds,
                format!(
                    "Trying to split a neuron with argument {} e8s. This is too little: \
                      at the minimum, one needs the minimum neuron stake, which is {} e8s, \
                      plus the transaction fee, which is {}. Hence the minimum split amount is {}.",
                    split.amount_e8s,
                    min_stake,
                    transaction_fee_e8s,
                    min_stake + transaction_fee_e8s
                ),
            ));
        }

        if parent_neuron.stake_e8s() < min_stake + split.amount_e8s {
            return Err(GovernanceError::new_with_message(
                ErrorType::InsufficientFunds,
                format!(
                    "Trying to split {} e8s out of neuron {}. \
                     This is not allowed, because the parent has stake {} e8s. \
                     If the requested amount was subtracted from it, there would be less than \
                     the minimum allowed stake, which is {} e8s. ",
                    split.amount_e8s,
                    parent_nid,
                    parent_neuron.stake_e8s(),
                    min_stake
                ),
            ));
        }
```

**File:** rs/sns/governance/src/governance.rs (L2144-2146)
```rust
            Action::ManageNervousSystemParameters(params) => {
                self.perform_manage_nervous_system_parameters(params)
            }
```

**File:** rs/sns/governance/src/governance.rs (L2579-2617)
```rust
    /// Executes a ManageNervousSystemParameters proposal by updating Governance's
    /// NervousSystemParameters
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

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L648-658)
```text
    // Change the nervous system's parameters.
    // Note that a change of a parameter will only affect future actions where
    // this parameter is relevant.
    // For example, NervousSystemParameters::neuron_minimum_stake_e8s specifies the
    // minimum amount of stake a neuron must have, which is checked at the time when
    // the neuron is created. If this NervousSystemParameter is decreased, all neurons
    // created after this change will have at least the new minimum stake. However,
    // neurons created before this change may have less stake.
    //
    // Id = 2.
    NervousSystemParameters manage_nervous_system_parameters = 6;
```

**File:** rs/sns/governance/api/src/ic_sns_governance.pb.v1.rs (L1146-1165)
```rust
/// The nervous system's parameters, which are parameters that can be changed, via proposals,
/// by each nervous system community.
/// For some of the values there are specified minimum values (floor) or maximum values
/// (ceiling). The motivation for this is a) to prevent that the nervous system accidentally
/// chooses parameters that result in an un-upgradable (and thus stuck) governance canister
/// and b) to prevent the canister from growing too big (which could harm the other canisters
/// on the subnet).
///
/// Required invariant: the canister code assumes that all system parameters are always set.
#[derive(Default, candid::CandidType, candid::Deserialize, Debug, Clone, PartialEq)]
pub struct NervousSystemParameters {
    /// The number of e8s (10E-8 of a token) that a rejected
    /// proposal costs the proposer.
    pub reject_cost_e8s: Option<u64>,
    /// The minimum number of e8s (10E-8 of a token) that can be staked in a neuron.
    ///
    /// To ensure that staking and disbursing of the neuron work, the chosen value
    /// must be larger than the transaction_fee_e8s.
    pub neuron_minimum_stake_e8s: Option<u64>,
    /// The transaction fee that must be paid for ledger transactions (except
```
