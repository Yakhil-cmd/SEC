### Title
Unprivileged User Can Exhaust SNS Governance `max_number_of_neurons` Limit, Blocking Legitimate Stakers - (File: `rs/sns/governance/src/governance.rs`)

### Summary

SNS Governance enforces a hard cap (`max_number_of_neurons`) on the total number of neurons. An unprivileged user can fill this cap by claiming many neurons with the minimum stake (`neuron_minimum_stake_e8s`), permanently blocking all other users from staking. Unlike the NNS Governance, SNS Governance has no rate limiter on neuron creation, making this a low-cost, persistent DoS.

### Finding Description

In SNS Governance, `claim_neuron` first calls `self.add_neuron(neuron.clone())?` which checks `check_neuron_population_can_grow`, then makes an async ledger call to verify the balance. If the balance is below `neuron_minimum_stake_e8s`, the neuron is removed and an error is returned. However, a successful claim only requires staking exactly `neuron_minimum_stake_e8s` tokens — the minimum is set by the SNS deployer and can be as low as `transaction_fee_e8s + 1` e8s. [1](#0-0) 

The `check_neuron_population_can_grow` function enforces a hard ceiling: [2](#0-1) 

The default `max_number_of_neurons` is 200,000: [3](#0-2) 

Critically, SNS Governance has **no rate limiter** on `claim_neuron`. Compare this to NNS Governance, which has an explicit `rate_limiter.try_reserve(...)` call in both `claim_neuron` and `split_neuron`: [4](#0-3) [5](#0-4) 

SNS Governance's `claim_neuron` has no equivalent protection: [6](#0-5) 

An attacker with enough SNS tokens to stake `max_number_of_neurons` neurons at `neuron_minimum_stake_e8s` each can permanently fill the neuron table. The attacker retains full ownership of all neurons and can disburse the tokens at any time, making the attack nearly free (only transaction fees are lost). The `neuron_minimum_stake_e8s` floor only requires it to be greater than `transaction_fee_e8s`: [7](#0-6) 

### Impact Explanation

Once `max_number_of_neurons` is reached, no new neurons can be created by any user: [8](#0-7) 

This blocks all new staking, preventing legitimate users from participating in SNS governance. The `claim_swap_neurons` path (used during SNS swaps) is also blocked, returning `MemoryExhausted` status: [9](#0-8) 

This means an attacker can also block an SNS decentralization swap from distributing neurons to participants, effectively sabotaging the entire SNS launch.

### Likelihood Explanation

The attack is reachable by any unprivileged user who can acquire SNS tokens. The cost is `max_number_of_neurons * neuron_minimum_stake_e8s` tokens, which are fully recoverable by disbursing the neurons. The only permanent cost is transaction fees. For SNS instances with low `neuron_minimum_stake_e8s` (e.g., 1 e8s as seen in test configs), the attack is essentially free. Even at the default of 1 SNS token per neuron and 200,000 neurons, the attacker only needs to hold 200,000 tokens temporarily. There is no rate limiting, no per-principal neuron count limit, and no minimum lock-up period enforced before disbursement.

### Recommendation

1. **Add a per-principal neuron count limit** to prevent a single principal from claiming a disproportionate share of the neuron table.
2. **Add a rate limiter** on `claim_neuron` in SNS Governance, analogous to the `InMemoryRateLimiter` used in NNS Governance.
3. **Enforce a meaningful minimum stake** with a floor that makes bulk neuron creation economically prohibitive.
4. Consider requiring a minimum dissolve delay before a neuron can be disbursed, to increase the capital cost of the attack.

### Proof of Concept

1. Deploy an SNS with `max_number_of_neurons = 200_000` and `neuron_minimum_stake_e8s = 100_000_000` (1 token).
2. Attacker acquires 200,000 SNS tokens.
3. Attacker calls `manage_neuron` with `ClaimOrRefresh` 200,000 times, each time using a different memo, staking exactly 1 token per neuron.
4. All 200,000 neuron slots are now occupied by the attacker.
5. Any subsequent call by a legitimate user to `manage_neuron::ClaimOrRefresh` returns `PreconditionFailed: "Cannot add neuron. Max number of neurons reached."` — confirmed by the existing test: [10](#0-9) 

6. Attacker calls `disburse` on all neurons to recover their tokens, paying only transaction fees.
7. The SNS governance is permanently DoS'd until the community passes a governance proposal to increase `max_number_of_neurons` — which itself requires existing neuron holders to vote, and may not be possible if the attacker controls enough voting power.

### Citations

**File:** rs/sns/governance/src/governance.rs (L4319-4385)
```rust
    async fn claim_neuron(
        &mut self,
        neuron_id: NeuronId,
        principal_id: &PrincipalId,
    ) -> Result<(), GovernanceError> {
        let now = self.env.now();

        // We need to create the neuron before checking the balance so that we record
        // the neuron and add it to the set of neurons with ongoing operations. This
        // avoids a race where a user calls this method a second time before the first
        // time responds. If we store the neuron and lock it before we make the call,
        // we know that any concurrent call to mutate the same neuron will need to wait
        // for this one to finish before proceeding.
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

**File:** rs/sns/governance/src/governance.rs (L4535-4547)
```rust
            match self.add_neuron(neuron) {
                Ok(()) => swap_neurons.push(SwapNeuron::from_neuron_recipe(
                    neuron_recipe,
                    ClaimedSwapNeuronStatus::Success,
                )),
                Err(err) => {
                    log!(ERROR, "Failed to claim Swap Neuron due to {:?}", err);
                    swap_neurons.push(SwapNeuron::from_neuron_recipe(
                        neuron_recipe,
                        ClaimedSwapNeuronStatus::MemoryExhausted,
                    ))
                }
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

**File:** rs/nns/governance/src/governance.rs (L1261-1270)
```rust
fn new_rate_limiter() -> InMemoryRateLimiter<String> {
    RateLimiter::new_in_memory(RateLimiterConfig {
        add_capacity_amount: 1,
        add_capacity_interval: Duration::from_secs(MINIMUM_SECONDS_BETWEEN_ALLOWANCE_INCREASE),
        max_capacity: MAX_NEURON_CREATION_SPIKE,
        // It should not be possible to have more than MAX_NEURON_CREATION_SPIKE_RESERVATIONS
        // because there is only one reservation space being used.
        // But we don't want to hit that error, so we add an extra one.
        max_reservations: MAX_NEURON_CREATION_SPIKE + 1,
    })
```

**File:** rs/nns/governance/src/governance.rs (L5992-5996)
```rust
        let neuron_limit_reservation = self.rate_limiter.try_reserve(
            self.env.now_system_time(),
            NEURON_RATE_LIMITER_KEY.to_string(),
            1,
        )?;
```

**File:** rs/sns/integration_tests/src/neuron.rs (L436-503)
```rust
fn test_claim_neuron_fails_when_max_number_of_neurons_is_reached() {
    local_test_on_sns_subnet(|runtime| async move {
        // Set up an SNS with a ledger account for two users
        let user1 = UserInfo::new(Sender::from_keypair(&TEST_USER1_KEYPAIR));
        let user2 = UserInfo::new(Sender::from_keypair(&TEST_USER2_KEYPAIR));
        let alloc = Tokens::from_tokens(1000).unwrap();

        let sys_params = NervousSystemParameters {
            neuron_claimer_permissions: Some(NeuronPermissionList {
                permissions: NeuronPermissionType::all(),
            }),
            max_number_of_neurons: Some(1),
            ..NervousSystemParameters::with_default_values()
        };

        let sns_init_payload = SnsTestsInitPayloadBuilder::new()
            .with_ledger_account(user1.sender.get_principal_id().0.into(), alloc)
            .with_ledger_account(user2.sender.get_principal_id().0.into(), alloc)
            .with_nervous_system_parameters(sys_params.clone())
            .build();

        let sns_canisters = SnsCanisters::set_up(&runtime, sns_init_payload).await;

        // Successfully STAKE and CLAIM user1's neuron, reaching the configured maximum number of
        // neurons allowed in the system
        sns_canisters
            .stake_and_claim_neuron(&user1.sender, Some(ONE_YEAR_SECONDS as u32))
            .await;

        // Only STAKE for user2.
        sns_canisters
            .stake_neuron_account(&user2.sender, &user2.subaccount, 1)
            .await;

        let manage_neuron_command = ManageNeuron {
            subaccount: user2.subaccount.to_vec(),
            command: Some(Command::ClaimOrRefresh(ClaimOrRefresh {
                by: Some(By::MemoAndController(MemoAndController {
                    memo: NONCE,
                    controller: None,
                })),
            })),
        };

        // Try claiming the Neuron for user2 (via memo and controller). This should fail due to
        // the max_number_of_neurons being reached.
        let response: ManageNeuronResponse = sns_canisters
            .governance
            .update_from_sender(
                "manage_neuron",
                candid_one,
                manage_neuron_command.clone(),
                &user2.sender,
            )
            .await
            .expect("Error calling the manage_neuron api.");

        // assert that the error_type is PreconditionFailed.
        let error = match response.command.unwrap() {
            CommandResponse::Error(error) => error,
            CommandResponse::ClaimOrRefresh(_) => {
                panic!(
                    "User should not have been able to claim a neuron due to reaching max_number_of_neurons"
                )
            }
            _ => panic!("Unexpected command response when claiming neuron."),
        };
        assert_eq!(error.error_type, ErrorType::PreconditionFailed as i32);
```
