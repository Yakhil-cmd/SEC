### Title
SNS Governance Neuron Slot Exhaustion DoS Blocks New Participant Staking - (File: `rs/sns/governance/src/governance.rs`)

### Summary
An unprivileged attacker can fill all available SNS neuron slots by creating `max_number_of_neurons` neurons with minimum stake and zero dissolve delay. Since neurons with zero dissolve delay are immediately dissolved (no voting power, no governance contribution), they act as "zombie" entries that permanently occupy slots without contributing. New users cannot create neurons until the attacker voluntarily disburses, blocking new governance participation. The staked tokens are fully recoverable by the attacker at any time, making the net cost only transaction fees.

### Finding Description
The `check_neuron_population_can_grow()` function enforces a global `max_number_of_neurons` ceiling with no per-principal limit and no minimum dissolve delay requirement for neuron creation. [1](#0-0) 

When `claim_neuron` is called, a new neuron is inserted with `DissolveDelaySeconds(0)` by default: [2](#0-1) 

A neuron with `DissolveDelaySeconds(0)` is immediately in the `Dissolved` state — it has zero voting power and contributes nothing to governance — yet it permanently occupies a slot in `self.proto.neurons` and is counted by `check_neuron_population_can_grow()`. The attacker can hold these dissolved neurons indefinitely without disbursing, since disbursement is voluntary. The SNS default ceiling is 200,000 neurons: [3](#0-2) 

For SNS instances with lower configured `max_number_of_neurons` and cheap tokens, the attack cost is minimal. The staked tokens are fully recoverable via `Disburse` at any time, so the attacker's only real cost is transaction fees.

### Impact Explanation
**Medium**: New users cannot stake SNS tokens and create neurons, permanently blocking new governance participation until the attacker voluntarily disburses. Existing neurons retain their voting power and can submit a governance proposal to increase `max_number_of_neurons`, but this requires the existing neuron holders to act and the proposal to pass — a non-trivial remediation path. For SNS instances where the attacker's zombie neurons represent a large fraction of total neurons, the governance quorum dynamics may also be affected.

### Likelihood Explanation
**Low**: The attacker must acquire and temporarily lock `max_number_of_neurons × neuron_minimum_stake_e8s` SNS tokens. For SNS instances with a low configured `max_number_of_neurons` (e.g., 100) and a low token price, the capital requirement can be negligible. The tokens are fully recoverable, so the attacker bears no permanent financial loss beyond transaction fees. The attacker gains nothing directly, analogous to the original report.

### Recommendation
1. Enforce a minimum dissolve delay at neuron creation time (e.g., require at least `neuron_minimum_dissolve_delay_to_vote_seconds`) so that neurons occupying slots must have meaningful governance participation.
2. Add a per-principal neuron count limit to prevent a single actor from filling all slots.
3. Add an SNS governance action allowing the community to forcibly disburse dissolved neurons that have been inactive beyond a configurable threshold, analogous to the admin deactivation function recommended in the original report.

### Proof of Concept
1. Attacker identifies an SNS with a low `max_number_of_neurons` (e.g., 100) and cheap SNS tokens.
2. Attacker transfers `max_number_of_neurons × neuron_minimum_stake_e8s` tokens to `max_number_of_neurons` distinct subaccounts of the SNS governance canister (one per neuron staking account).
3. Attacker calls `manage_neuron` → `ClaimOrRefresh` for each subaccount. Each call reaches `claim_neuron`, which inserts a neuron with `DissolveDelaySeconds(0)` — immediately dissolved, zero voting power. [4](#0-3) 

4. After `max_number_of_neurons` neurons are created, `check_neuron_population_can_grow()` returns `PreconditionFailed` for any subsequent `ClaimOrRefresh` call by any user. [5](#0-4) 

5. The attacker holds the dissolved neurons without disbursing. New users attempting to stake and participate in SNS governance receive `"Cannot add neuron. Max number of neurons reached."` indefinitely.
6. The attacker can recover all staked tokens at any time by calling `Disburse` on each neuron (they are already in the `Dissolved` state), making the net cost only the transaction fees paid during creation and disbursement.

### Citations

**File:** rs/sns/governance/src/governance.rs (L4332-4356)
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
```

**File:** rs/sns/governance/src/governance.rs (L4358-4385)
```rust
        // This also verifies that there are not too many neurons already.
        self.add_neuron(neuron.clone())?;

        // Get the balance of the neuron's subaccount from ledger canister.
        let subaccount = neuron_id.subaccount()?;
        let account = self.neuron_account_id(subaccount);
        let balance = self.ledger.account_balance(account).await?;

        let min_stake = self
            .nervous_system_parameters_or_panic()
            .neuron_minimum_stake_e8s
            .expect("NervousSystemParameters must have neuron_minimum_stake_e8s");

        if balance.get_e8s() < min_stake {
            // To prevent this method from creating non-staked
            // neurons, we must also remove the neuron that was
            // previously created.
            self.remove_neuron(&neuron_id, neuron)?;
            return Err(GovernanceError::new_with_message(
                ErrorType::InsufficientFunds,
                format!(
                    "Account does not have enough funds to stake a neuron. \
                     Please make sure that account has at least {:?} e8s (was {:?} e8s)",
                    min_stake,
                    balance.get_e8s()
                ),
            ));
        }
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
