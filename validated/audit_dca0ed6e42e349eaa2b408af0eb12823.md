### Title
SNS Governance Neuron Slot Exhaustion via Minimum-Stake Claim and Immediate Disburse - (File: rs/sns/governance/src/governance.rs)

### Summary
SNS Governance enforces a hard cap `max_number_of_neurons` on the total number of neurons. An unprivileged attacker can permanently fill all neuron slots at a cost of only transaction fees by repeatedly: (1) claiming a neuron with the minimum stake and dissolve delay = 0 (immediately dissolved), then (2) immediately disbursing the neuron to recover the stake. The disbursed neuron is never removed from `self.proto.neurons`, so it permanently occupies a slot. Once all slots are filled, no new neurons can be created in the SNS.

### Finding Description

SNS Governance's `claim_neuron` creates a neuron with `dissolve_state: Some(DissolveState::DissolveDelaySeconds(0))`, placing it immediately in the `Dissolved` state. [1](#0-0) 

The `add_neuron` call inside `claim_neuron` increments the neuron count checked by `check_neuron_population_can_grow`: [2](#0-1) 

Because the neuron is immediately dissolved (dissolve delay = 0), `disburse_neuron` succeeds right away, transferring the stake back to the attacker. However, `disburse_neuron` does **not** call `remove_neuron` — it only zeroes `cached_neuron_stake_e8s`. The neuron remains in `self.proto.neurons` permanently: [3](#0-2) 

The neuron count used by `check_neuron_population_can_grow` counts all entries in `self.proto.neurons`, including 0-stake zombie neurons: [4](#0-3) 

The default and ceiling for `max_number_of_neurons` in SNS is 200,000: [5](#0-4) 

Unlike NNS Governance, SNS `claim_neuron` has **no rate limiter** on neuron creation. NNS has `MAX_SUSTAINED_NEURONS_PER_HOUR = 15` and `MAX_NEURON_CREATION_SPIKE = 300` which makes the equivalent attack impractical there: [6](#0-5) 

SNS has no such protection.

### Impact Explanation

An attacker fills all 200,000 neuron slots with zombie neurons. After the attack, `check_neuron_population_can_grow` returns an error for every subsequent `claim_neuron` call: [4](#0-3) 

No new principals can join SNS governance. Existing token holders who have not yet created a neuron are permanently locked out of voting, proposal submission, and reward accrual. The SNS community's only recourse is a governance proposal to increase `max_number_of_neurons`, but this requires existing neuron holders to pass it — and if the attacker's zombie neurons dilute quorum or the SNS is young with few existing neurons, even this recovery path may be impaired.

### Likelihood Explanation

The net cost to the attacker is approximately `max_number_of_neurons × 2 × transaction_fee_e8s`. With SNS defaults (200,000 neurons, `transaction_fee_e8s = 10,000 e8s`), this is `200,000 × 2 × 10,000 = 4,000,000,000 e8s = 40 governance tokens` in fees. The attacker recovers all staked principal. There is no rate limiter in SNS `claim_neuron`. The attack is fully automatable via ingress calls to `manage_neuron` with `ClaimOrRefresh` followed by `Disburse`. Any principal with 40 governance tokens and access to the SNS canister can execute this.

### Recommendation

1. **Remove neurons on full disbursal**: When `disburse_neuron` reduces `cached_neuron_stake_e8s` to zero (or below `transaction_fee_e8s`), call `remove_neuron` to free the slot.
2. **Add a rate limiter to SNS `claim_neuron`**: Mirror the NNS `MAX_SUSTAINED_NEURONS_PER_HOUR` / `MAX_NEURON_CREATION_SPIKE` rate limiter in SNS governance.
3. **Enforce a minimum dissolve delay for neuron creation**: Require a non-zero dissolve delay so that stake cannot be immediately recovered, raising the economic cost of the attack.

### Proof of Concept

```
For i in 1..max_number_of_neurons:
  1. Transfer neuron_minimum_stake_e8s tokens to governance subaccount(principal_i, memo_i)
  2. Call manage_neuron(ClaimOrRefresh { by: MemoAndController { memo: memo_i, controller: principal_i } })
     → claim_neuron() creates neuron with DissolveDelaySeconds(0) → immediately Dissolved
     → add_neuron() increments proto.neurons count
  3. Call manage_neuron(Disburse { amount: None, to_account: None })
     → disburse_neuron() transfers stake back to attacker
     → proto.neurons entry remains with cached_neuron_stake_e8s = 0
     → slot permanently occupied

After loop:
  Any call to manage_neuron(ClaimOrRefresh { ... }) by a new user returns:
  GovernanceError { error_type: PreconditionFailed, error_message: "Cannot add neuron. Max number of neurons reached." }

Net attacker cost: ~40 governance tokens in transaction fees (fully recoverable principal).
```

### Citations

**File:** rs/sns/governance/src/governance.rs (L1119-1160)
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
```

**File:** rs/sns/governance/src/governance.rs (L4332-4359)
```rust
        let neuron = Neuron {
            id: Some(neuron_id.clone()),
            permissions: vec![NeuronPermission::new(
                principal_id,
                self.neuron_claimer_permissions_or_panic().permissions,
            )],
            cached_neuron_stake_e8s: 0,
            neuron_fees_e8s: 0,
            created_timestamp_seconds: now,
            aging_since_timestamp_seconds: now,
            followees: self.default_followees_or_panic().followees,
            topic_followees: Some(TopicFollowees {
                topic_id_to_followees: btreemap! {},
            }),
            maturity_e8s_equivalent: 0,
            dissolve_state: Some(DissolveState::DissolveDelaySeconds(0)),
            // A neuron created through the `claim_or_refresh` ManageNeuron command will
            // have the default voting power multiplier applied.
            voting_power_percentage_multiplier: DEFAULT_VOTING_POWER_PERCENTAGE_MULTIPLIER,
            source_nns_neuron_id: None,
            staked_maturity_e8s_equivalent: None,
            auto_stake_maturity: None,
            vesting_period_seconds: None,
            disburse_maturity_in_progress: vec![],
        };

        // This also verifies that there are not too many neurons already.
        self.add_neuron(neuron.clone())?;
```

**File:** rs/sns/governance/src/governance.rs (L6363-6379)
```rust
    /// Checks whether new neurons can be added or whether the maximum number of neurons,
    /// as defined in the nervous system parameters, has already been reached.
    fn check_neuron_population_can_grow(&self) -> Result<(), GovernanceError> {
        let max_number_of_neurons = self
            .nervous_system_parameters_or_panic()
            .max_number_of_neurons
            .expect("NervousSystemParameters must have max_number_of_neurons");

        if (self.proto.neurons.len() as u64) + 1 > max_number_of_neurons {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Cannot add neuron. Max number of neurons reached.",
            ));
        }

        Ok(())
    }
```

**File:** rs/sns/governance/src/types.rs (L383-386)
```rust
    /// This is an upper bound for `max_number_of_neurons`. Exceeding it may cause
    /// degradation in the governance canister or the subnet hosting the SNS.
    /// See also: `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS`.
    pub const MAX_NUMBER_OF_NEURONS_CEILING: u64 = 200_000;
```

**File:** rs/nns/governance/src/governance.rs (L231-240)
```rust
// Spawning is exempted from rate limiting, so we don't need large of a limit here.
pub const MAX_SUSTAINED_NEURONS_PER_HOUR: u64 = 15;

pub const MINIMUM_SECONDS_BETWEEN_ALLOWANCE_INCREASE: u64 = 3600 / MAX_SUSTAINED_NEURONS_PER_HOUR;

/// The maximum number of neurons that can be created in a spike. Note that such rate of neuron
/// creation is not sustainable as the allowance will be exhausted after creating this many neurons
/// in a short period of time, and the allowance will only be increased according to
/// `MINIMUM_SECONDS_BETWEEN_ALLOWANCE_INCREASE`.
pub const MAX_NEURON_CREATION_SPIKE: u64 = MAX_SUSTAINED_NEURONS_PER_HOUR * 20;
```
