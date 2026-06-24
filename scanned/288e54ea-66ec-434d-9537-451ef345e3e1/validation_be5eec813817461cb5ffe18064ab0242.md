### Title
SNS Governance `ManageNervousSystemParameters` Raising `neuron_minimum_stake_e8s` Retroactively Blocks `split_neuron` for Existing Neurons, Locking Staked Funds Until Dissolve Delay Expires - (`File: rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS governance system allows any governance proposal to raise `NervousSystemParameters.neuron_minimum_stake_e8s` via `ManageNervousSystemParameters`. When this parameter is raised, the `split_neuron` function applies the **new** minimum retroactively to **existing** neurons. A neuron whose stake falls between the old and new minimum can no longer be split to create a child neuron with a shorter dissolve delay. The neuron holder is forced to wait for the full original dissolve delay to expire before accessing their funds — directly analogous to the Babylon "funds locked for the entire staking period" scenario. The SNS proto documentation explicitly states that parameter changes "will only affect future actions," but the code does not enforce this invariant for split operations.

---

### Finding Description

The `ManageNervousSystemParameters` action in SNS governance is executable by any governance participant who can pass a proposal: [1](#0-0) 

The proto comment explicitly documents the intended design:

> "Note that a change of a parameter will only affect future actions where this parameter is relevant. For example, `NervousSystemParameters::neuron_minimum_stake_e8s` specifies the minimum amount of stake a neuron must have, which is checked at the time when the neuron is created. If this NervousSystemParameter is decreased, all neurons created after this change will have at least the new minimum stake. However, neurons created before this change may have less stake." [2](#0-1) 

Despite this documented intent, `split_neuron` reads `neuron_minimum_stake_e8s` from the **current** `NervousSystemParameters` at call time: [3](#0-2) 

Both guards enforce the current minimum:

1. The split amount must be `>= min_stake + transaction_fee_e8s`
2. The remaining parent stake must be `>= min_stake`

If `neuron_minimum_stake_e8s` is raised from X to Y (where X < Y), any neuron with stake in the range `[X, 2Y + fee)` cannot be split at all — because no valid split amount exists that satisfies both constraints simultaneously.

The parameter update itself is applied without any backward-compatibility check on existing neurons: [4](#0-3) 

Similarly, `refresh_neuron` blocks neurons whose on-chain balance is below the new minimum: [5](#0-4) 

---

### Impact Explanation

**Governance authorization bug / ledger conservation bug.**

A neuron holder who staked exactly at the old minimum (e.g., 100 e8s) with a long dissolve delay (e.g., 4 years) loses the ability to split their neuron after `neuron_minimum_stake_e8s` is raised to 200 e8s. Splitting is the only mechanism to create a child neuron with a shorter dissolve delay, allowing partial early exit. Without it, the holder must wait the full 4-year dissolve delay to access any funds. The neuron can still be dissolved and disbursed in full, but the partial-exit path is permanently blocked for the lifetime of the dissolve delay.

Additionally, `refresh_neuron` fails for any neuron whose balance is below the new minimum, preventing stake synchronization between the ledger and governance state.

- **Severity**: Medium
- **Scope**: Any SNS instance where governance raises `neuron_minimum_stake_e8s`

---

### Likelihood Explanation

**Likelihood**: Medium-High.

The `ManageNervousSystemParameters` proposal type is a standard, routine governance action available to any SNS token holder with sufficient voting power. Raising `neuron_minimum_stake_e8s` is a legitimate governance decision (e.g., to prevent spam neurons or adjust economics). There is no mechanism in the proposal validation or execution path that checks whether existing neurons would be retroactively affected. The proto comment documents the intended backward-compatibility guarantee, but the code does not enforce it, making accidental breakage likely as SNS communities evolve their parameters over time. [6](#0-5) 

The validation only checks that the new value is self-consistent (greater than `transaction_fee_e8s`), not that it is backward-compatible with existing neurons.

---

### Recommendation

1. **Enforce the documented invariant**: In `split_neuron` and `refresh_neuron`, use the minimum stake that was in effect at the time the neuron was created (stored per-neuron), rather than the current global parameter.

2. **Alternatively**: Add a pre-execution check in `perform_manage_nervous_system_parameters` that scans existing neurons and rejects the proposal if any neuron's stake would fall below the new minimum, or emits a warning.

3. **At minimum**: Update the proto comment to accurately reflect that the invariant is NOT currently enforced for split and refresh operations, so SNS communities are aware of the risk before raising this parameter.

---

### Proof of Concept

**Setup**:
- SNS is initialized with `neuron_minimum_stake_e8s = 100_000_000` (1 token).
- Alice stakes exactly 1 token and claims a neuron with a 4-year dissolve delay.

**Attack**:
1. A governance proposal is submitted to raise `neuron_minimum_stake_e8s` to `200_000_000` (2 tokens). This is a legitimate governance action.
2. The proposal passes.
3. `perform_manage_nervous_system_parameters` updates `self.proto.parameters` with the new value. [7](#0-6) 

4. Alice attempts to split her neuron (e.g., to create a child with a 1-year dissolve delay):
   - Any split amount `a` must satisfy `a >= 200_000_000 + fee` AND `100_000_000 - a >= 200_000_000`.
   - The second constraint requires `a <= -100_000_000`, which is impossible.
   - `split_neuron` returns `InsufficientFunds`. [8](#0-7) 

5. Alice cannot partially exit her position. She must wait 4 years for the full dissolve delay to expire before disbursing her 1 token.

The `disburse_neuron` path (full exit after dissolve) remains functional, confirming funds are not permanently lost — but the partial-exit mechanism is permanently blocked for the duration of the dissolve delay, directly mirroring the Babylon "locked for the entire staking period" impact. [9](#0-8)

### Citations

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

**File:** rs/sns/governance/src/governance.rs (L1119-1136)
```rust
    pub async fn disburse_neuron(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        disburse: &manage_neuron::Disburse,
    ) -> Result<u64, GovernanceError> {
        // First check authorized
        let neuron = self.get_neuron_result(id)?;
        neuron.check_authorized(caller, NeuronPermissionType::Disburse)?;

        // Check that the neuron is dissolved.
        let state = neuron.state(self.env.now());
        if state != NeuronState::Dissolved {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!("Neuron {id} is NOT dissolved. It is in state {state:?}"),
            ));
        }
```

**File:** rs/sns/governance/src/governance.rs (L1301-1331)
```rust
        let min_stake = self
            .proto
            .parameters
            .as_ref()
            .expect("Governance must have NervousSystemParameters.")
            .neuron_minimum_stake_e8s
            .expect("NervousSystemParameters must have neuron_minimum_stake_e8s");

        let transaction_fee_e8s = self.transaction_fee_e8s_or_panic();

        // Get the neuron and clone to appease the borrow checker.
        // We'll get a mutable reference when we need to change it later.
        let parent_neuron = self.get_neuron_result(id)?.clone();
        let parent_nid = parent_neuron.id.as_ref().expect("Neurons must have an id");

        parent_neuron.check_authorized(caller, NeuronPermissionType::Split)?;

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

**File:** rs/sns/governance/src/governance.rs (L4258-4272)
```rust
        let min_stake = self
            .nervous_system_parameters_or_panic()
            .neuron_minimum_stake_e8s
            .expect("NervousSystemParameters must have neuron_minimum_stake_e8s");
        if balance.get_e8s() < min_stake {
            return Err(GovernanceError::new_with_message(
                ErrorType::InsufficientFunds,
                format!(
                    "Account does not have enough funds to refresh a neuron. \
                        Please make sure that account has at least {:?} e8s (was {:?} e8s)",
                    min_stake,
                    balance.get_e8s()
                ),
            ));
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
