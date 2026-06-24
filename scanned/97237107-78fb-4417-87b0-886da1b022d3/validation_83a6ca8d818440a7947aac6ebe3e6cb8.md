### Title
Insufficient Validation of `transaction_fee_e8s` in SNS `NervousSystemParameters` Allows Governance to Permanently Lock All Neuron Funds - (File: `rs/sns/governance/src/types.rs`)

### Summary
The `validate_transaction_fee_e8s` function in SNS governance only checks that the field is present (not `None`), with no upper bound. A governance proposal setting `transaction_fee_e8s` to an extreme value while keeping `neuron_minimum_stake_e8s` marginally higher passes all validation, but renders every existing neuron unable to disburse — permanently locking staked tokens.

### Finding Description
In `rs/sns/governance/src/types.rs`, `validate_transaction_fee_e8s` performs only a presence check: [1](#0-0) 

`validate_neuron_minimum_stake_e8s` only enforces `neuron_minimum_stake_e8s > transaction_fee_e8s`: [2](#0-1) 

There is no ceiling on either value. A proposal setting `transaction_fee_e8s = u64::MAX - 1` and `neuron_minimum_stake_e8s = u64::MAX` passes `NervousSystemParameters::validate()` cleanly: [3](#0-2) 

At disbursal time, SNS governance reads `transaction_fee_e8s` directly from the live parameters: [4](#0-3) 

The disbursal logic then passes this value as the ledger fee: [5](#0-4) [6](#0-5) 

When `transaction_fee_e8s` exceeds the neuron's actual on-ledger balance, the ICRC-1 ledger rejects the transfer (`amount + fee > balance`). Because the governance canister propagates this error upward, every `disburse_neuron` call fails, and the staked tokens are permanently inaccessible.

The same unbounded `transaction_fee_e8s` is accepted by `SnsInitPayload::validate_transaction_fee_e8s` at SNS creation time: [7](#0-6) 

### Impact Explanation
**High.** Every neuron holder in the affected SNS loses the ability to disburse their staked tokens. Because the governance canister is the sole custodian of neuron subaccounts, and because the fee is applied at the ledger layer, there is no bypass path. Staked tokens are effectively frozen for all participants whose stake is below the new fee threshold — which, at `u64::MAX - 1`, is every neuron in existence.

### Likelihood Explanation
**Low.** Exploiting this requires a `ManageNervousSystemParameters` proposal to pass with a majority of SNS voting power. This is analogous to the original report's "configuration error by Governance or them being malicious or compromised." The root cause is the missing upper-bound check in production validation code, not the governance process itself.

### Recommendation
Add a sensible ceiling to `validate_transaction_fee_e8s` — for example, capping it at a small fraction of `neuron_minimum_stake_e8s` or at an absolute maximum (e.g., `10_000_000` e8s = 0.1 tokens). The same ceiling should be enforced in `SnsInitPayload::validate_transaction_fee_e8s`. Analogously, `reward_rate_transition_duration_seconds` in `VotingRewardsParameters` is validated with an unbounded range `0..` while `round_duration_seconds` is correctly bounded by `MAX_REWARD_ROUND_DURATION_SECONDS`; a matching upper bound should be added there as well: [8](#0-7) 

### Proof of Concept
1. An SNS governance majority submits a `ManageNervousSystemParameters` proposal with:
   - `transaction_fee_e8s = u64::MAX - 1`
   - `neuron_minimum_stake_e8s = u64::MAX`
2. `NervousSystemParameters::validate()` passes — `neuron_minimum_stake_e8s > transaction_fee_e8s` is satisfied, and no ceiling is checked.
3. The proposal is executed; the live parameters are updated.
4. Any neuron holder calls `disburse_neuron`. The governance canister reads `transaction_fee_e8s = u64::MAX - 1` and passes it to `ledger.transfer_funds(stake_e8s, u64::MAX - 1, ...)`.
5. The ICRC-1 ledger rejects the call: `stake_e8s + (u64::MAX - 1) > account_balance`.
6. All disbursal attempts fail. Every neuron's staked tokens are permanently locked inside the governance canister's subaccounts. [9](#0-8)

### Citations

**File:** rs/sns/governance/src/types.rs (L570-594)
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

**File:** rs/sns/governance/src/types.rs (L620-624)
```rust
    /// Validates that the nervous system parameter transaction_fee_e8s is well-formed.
    fn validate_transaction_fee_e8s(&self) -> Result<u64, String> {
        self.transaction_fee_e8s
            .ok_or_else(|| "NervousSystemParameters.transaction_fee_e8s must be set".to_string())
    }
```

**File:** rs/sns/governance/src/governance.rs (L1119-1172)
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

        let transaction_fee_e8s = self.transaction_fee_e8s_or_panic();

        let from_subaccount = neuron.subaccount()?;

        // If no account was provided, transfer to the caller's (default) account.
        let to_account = match disburse.to_account.as_ref() {
            None => Account {
                owner: caller.0,
                subaccount: None,
            },
            Some(ai_pb) => Account::try_from(ai_pb.clone()).map_err(|e| {
                GovernanceError::new_with_message(
                    ErrorType::InvalidCommand,
                    format!("The recipient's subaccount is invalid due to: {e}"),
                )
            })?,
        };

        let max_burnable_fee = self.maximum_burnable_fees_for_neuron(neuron)?;

        // Calculate the amount to transfer and make sure no matter what the user
        // disburses we still take the neuron management fees into account.
        let mut disburse_amount_e8s = disburse
            .amount
            .as_ref()
            .map_or(neuron.stake_e8s(), |a| a.e8s);

        // You cannot disburse more than the neuron's stake, which includes fees.
        disburse_amount_e8s = disburse_amount_e8s.min(neuron.stake_e8s());

        // Subtract the transaction fee from the amount to disburse since it will
        // be deducted from the source (the neuron's) account.
        if disburse_amount_e8s > transaction_fee_e8s {
            disburse_amount_e8s -= transaction_fee_e8s
        }
```

**File:** rs/sns/governance/src/governance.rs (L1214-1223)
```rust
        let block_height = self
            .ledger
            .transfer_funds(
                disburse_amount_e8s,
                transaction_fee_e8s,
                Some(from_subaccount),
                to_account,
                self.env.now(),
            )
            .await?;
```

**File:** rs/sns/init/src/lib.rs (L1003-1008)
```rust
    fn validate_transaction_fee_e8s(&self) -> Result<(), String> {
        match self.transaction_fee_e8s {
            Some(_) => Ok(()),
            None => Err("Error: transaction_fee_e8s must be specified.".to_string()),
        }
    }
```

**File:** rs/sns/governance/src/reward.rs (L262-268)
```rust
    fn reward_rate_transition_duration_seconds_defects(&self) -> Vec<String> {
        require_field_set_and_in_range(
            "reward_rate_transition_duration_seconds",
            &self.reward_rate_transition_duration_seconds,
            0..,
        )
    }
```
