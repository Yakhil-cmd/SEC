### Title
Monthly Node Provider Rewards Permanently Lost When ICP Ledger Is Stopped During Distribution - (File: `rs/nns/governance/src/governance.rs`)

### Summary
In `mint_monthly_node_provider_rewards`, the result of `reward_node_providers` is silently discarded with `let _ =`, and `update_most_recent_monthly_node_provider_rewards` is unconditionally called afterward. If the ICP ledger is stopped (e.g., during a routine NNS-governed upgrade) at the moment this function executes, all node provider minting transfers fail silently, the monthly reward timestamp is still advanced, and the rewards for that entire month are permanently unrecoverable — no retry will occur.

### Finding Description
`mint_monthly_node_provider_rewards` (rs/nns/governance/src/governance.rs) executes the following sequence:

1. Fetches the monthly reward amounts from the Node Reward Canister (NRC) via `get_node_providers_rewards().await?` or `get_monthly_node_provider_rewards().await?`.
2. Calls `reward_node_providers`, which iterates over all node providers and calls `transfer_funds` (a minting transfer from the governance minting account) on the ICP ledger for each provider.
3. **Discards the result** with `let _ = self.reward_node_providers(...).await`.
4. **Unconditionally** calls `update_most_recent_monthly_node_provider_rewards`, advancing the "last rewarded" timestamp regardless of whether any transfers succeeded. [1](#0-0) 

The ICP ledger canister can be stopped during a routine NNS-governed upgrade. When stopped, any inter-canister call to the ledger is rejected with a system-level error. If `mint_monthly_node_provider_rewards` runs while the ledger is stopped, every `transfer_funds` call inside `reward_node_providers` will fail. Because the error is discarded and the timestamp is advanced unconditionally, the governance heartbeat/timer will not retry the distribution for that month.

The `reward_node_providers` function itself iterates over all providers and calls `reward_node_provider_helper` for each, propagating the first error but continuing for subsequent providers: [2](#0-1) 

The minting transfer path goes through `mint_reward_to_neuron_or_account`, which calls `transfer_funds` on the ICP ledger: [3](#0-2) 

### Impact Explanation
High: All node provider rewards for the affected month are permanently lost. The unconditional call to `update_most_recent_monthly_node_provider_rewards` advances the timestamp, preventing any retry by the governance heartbeat. Node providers receive no ICP compensation for that month's infrastructure work, with no mechanism for recovery short of a manual governance proposal. [4](#0-3) 

### Likelihood Explanation
Low: The ICP ledger is only stopped briefly during routine NNS-governed upgrades. The probability of `mint_monthly_node_provider_rewards` executing during this brief window is low, but non-zero and increases with upgrade frequency. Unlike the EVM analog where a token pause is an explicit admin action, here the trigger is a routine operational event (canister upgrade) that is expected to occur regularly.

### Recommendation
Remove the `let _ =` pattern and propagate or handle the error from `reward_node_providers`. If the call fails (partially or fully), do **not** call `update_most_recent_monthly_node_provider_rewards`. Instead, allow the governance heartbeat to retry the distribution on the next cycle. Alternatively, record which providers were successfully rewarded and only advance the timestamp after all providers have been successfully rewarded, similar to how the ckBTC minter handles partial minting failures with `error_count` tracking. [5](#0-4) 

### Proof of Concept
1. NNS governance submits a routine proposal to upgrade the ICP ledger canister.
2. The ICP ledger is stopped briefly during the upgrade window.
3. During this window, the NNS governance heartbeat triggers `mint_monthly_node_provider_rewards`.
4. `get_node_providers_rewards().await?` succeeds (calls NRC, not ICP ledger).
5. `reward_node_providers` is called; each `transfer_funds` call to the stopped ICP ledger is rejected.
6. The error is discarded with `let _ =`.
7. `update_most_recent_monthly_node_provider_rewards` is called, advancing the monthly timestamp.
8. The next governance heartbeat sees the timestamp is recent (within the last month) and does not retry.
9. All node providers lose their monthly ICP rewards for that period with no recovery path. [6](#0-5)

### Citations

**File:** rs/nns/governance/src/governance.rs (L3842-3930)
```rust
    async fn mint_reward_to_neuron_or_account(
        &mut self,
        np_principal: &PrincipalId,
        reward: &RewardNodeProvider,
    ) -> Result<(), GovernanceError> {
        let now = self.env.now();
        match reward.reward_mode.as_ref() {
            None => Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Reward node provider proposal must have a reward mode.",
            )),
            Some(RewardMode::RewardToNeuron(reward_to_neuron)) => {
                let to_subaccount = Subaccount(self.randomness.random_byte_array()?);
                let _block_height = self
                    .ledger
                    .transfer_funds(
                        reward.amount_e8s,
                        0, // Minting transfers don't pay transaction fees.
                        None,
                        neuron_subaccount(to_subaccount),
                        now,
                    )
                    .await?;
                let nid = self.neuron_store.new_neuron_id(&mut *self.randomness)?;
                let dissolve_delay_seconds = std::cmp::min(
                    reward_to_neuron.dissolve_delay_seconds,
                    max_dissolve_delay_seconds(),
                );

                let dissolve_state_and_age = if dissolve_delay_seconds > 0 {
                    DissolveStateAndAge::NotDissolving {
                        dissolve_delay_seconds,
                        aging_since_timestamp_seconds: now,
                    }
                } else {
                    DissolveStateAndAge::DissolvingOrDissolved {
                        when_dissolved_timestamp_seconds: now,
                    }
                };

                // Transfer successful.
                let neuron = NeuronBuilder::new(
                    nid,
                    to_subaccount,
                    *np_principal,
                    dissolve_state_and_age,
                    now,
                )
                .with_followees(self.heap_data.default_followees.clone())
                .with_cached_neuron_stake_e8s(reward.amount_e8s)
                .with_kyc_verified(true)
                .build();

                self.add_neuron(nid.id, neuron)
            }
            Some(RewardMode::RewardToAccount(reward_to_account)) => {
                // We are not creating a neuron, just transferring funds.
                let to_account = match &reward_to_account.to_account {
                    Some(to_account) => AccountIdentifier::try_from(to_account).map_err(|e| {
                        GovernanceError::new_with_message(
                            ErrorType::InvalidCommand,
                            format!("The recipient's subaccount is invalid due to: {e}"),
                        )
                    })?,
                    None => AccountIdentifier::new(*np_principal, None),
                };

                self.ledger
                    .transfer_funds(
                        reward.amount_e8s,
                        0, // Minting transfers don't pay transaction fees.
                        None,
                        to_account,
                        now,
                    )
                    .await
                    .map(|_| ())
                    .map_err(|e| {
                        GovernanceError::new_with_message(
                            ErrorType::PreconditionFailed,
                            format!(
                                "Couldn't perform minting transfer: {}",
                                GovernanceError::from(e)
                            ),
                        )
                    })
            }
        }
    }
```

**File:** rs/nns/governance/src/governance.rs (L3987-4006)
```rust
    async fn reward_node_providers(
        &mut self,
        rewards: &[RewardNodeProvider],
    ) -> Result<(), GovernanceError> {
        let mut result = Ok(());

        for reward in rewards {
            let reward_result = self.reward_node_provider_helper(reward).await;
            if reward_result.is_err() {
                println!(
                    "Rewarding {:?} failed. Reason: {:}",
                    reward,
                    reward_result.clone().unwrap_err()
                );
            }
            result = result.or(reward_result);
        }

        result
    }
```

**File:** rs/nns/governance/src/governance.rs (L4040-4089)
```rust
    async fn mint_monthly_node_provider_rewards(&mut self) -> Result<(), GovernanceError> {
        // Return immediately if another call is already in progress.
        thread_local! {
            static LOCK: RefCell<Option<u64>> = const { RefCell::new(None) };
        }
        let release_on_drop = ic_nervous_system_lock::acquire(&LOCK, self.env.now());
        if let Err(earlier_call_start_timestamp) = release_on_drop {
            // Log, but not too frequently (at most once every 5 minutes).
            thread_local! {
                static LAST_LOGGED_UNAVAILABLE_TIMESTAMP_SECONDS: RefCell<u64> = const { RefCell::new(0) };
            }
            let time_since_logged_seconds = LAST_LOGGED_UNAVAILABLE_TIMESTAMP_SECONDS
                .with(|t| self.env.now().saturating_sub(*t.borrow()));
            if time_since_logged_seconds > 5 * 60 {
                println!(
                    "{}Another mint_monthly_node_provider_rewards call (started at \
                     {} seconds since the UNIX epoch) is already in progress.",
                    LOG_PREFIX, earlier_call_start_timestamp,
                );
                LAST_LOGGED_UNAVAILABLE_TIMESTAMP_SECONDS.with(|t| {
                    *t.borrow_mut() = self.env.now();
                });
            }

            return Ok(());
        }

        let monthly_node_provider_rewards = if are_performance_based_rewards_enabled() {
            self.get_node_providers_rewards().await?
        } else {
            self.get_monthly_node_provider_rewards().await?
        };

        let _ = self
            .reward_node_providers(&monthly_node_provider_rewards.rewards)
            .await;
        self.update_most_recent_monthly_node_provider_rewards(monthly_node_provider_rewards);

        // Commit the minting status by making a canister call.
        let _unused_canister_status_response = self
            .env
            .call_canister_method(
                GOVERNANCE_CANISTER_ID,
                "get_build_metadata",
                Encode!().unwrap_or_default(),
            )
            .await;

        Ok(())
    }
```

**File:** rs/nns/governance/src/governance.rs (L4091-4097)
```rust
    fn update_most_recent_monthly_node_provider_rewards(
        &mut self,
        most_recent_rewards: MonthlyNodeProviderRewards,
    ) {
        record_node_provider_rewards(most_recent_rewards.clone());
        self.heap_data.most_recent_monthly_node_provider_rewards = Some(most_recent_rewards);
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/reimbursement/mod.rs (L94-103)
```rust
            Err(err) => {
                log!(
                    Priority::Info,
                    "[reimburse_withdrawals]: Failed to reimburse {:?}: {:?}. Will retry later",
                    reimbursement,
                    err
                );
                error_count += 1;
            }
        }
```
