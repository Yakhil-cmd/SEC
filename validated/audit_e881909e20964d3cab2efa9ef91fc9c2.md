### Title
Missing Zero-Amount Check in Node Provider Reward Distribution Causes Spurious Ledger Minting Transactions and Zero-Stake Neuron Creation - (File: rs/nns/governance/src/governance.rs)

### Summary
The `get_node_provider_reward` function returns a `Some(RewardNodeProvider { amount_e8s: 0, ... })` when a node provider's XDR reward is zero, and the downstream `reward_node_provider_helper` function never checks whether `amount_e8s > 0` before proceeding to mint. This causes the governance canister to issue spurious zero-amount minting transfers to the ICP ledger and, in the `RewardToNeuron` path, to create zero-stake neurons in governance state.

### Finding Description

`get_node_provider_reward` computes `amount_e8s` by integer division:

```rust
let amount_e8s = ((xdr_permyriad_reward as u128 * TOKEN_SUBDIVIDABLE_BY as u128)
    / xdr_permyriad_per_icp as u128) as u64;
```

When `xdr_permyriad_reward == 0` (a node provider registered in governance but with no active nodes, or nodes with a zero reward rate), `amount_e8s` evaluates to `0`. The function still returns `Some(RewardNodeProvider { amount_e8s: 0, ... })` — it never guards against the zero case. [1](#0-0) 

Both callers — `get_monthly_node_provider_rewards` and `get_node_providers_rewards` — push this zero-reward entry into the rewards list unconditionally:

```rust
let xdr_permyriad_reward = *reg_rewards.get(np_id).unwrap_or(&0);
if let Some(reward_node_provider) =
    get_node_provider_reward(np, xdr_permyriad_reward, xdr_permyriad_per_icp)
{
    rewards.push(reward_node_provider);   // pushed even when amount_e8s == 0
}
``` [2](#0-1) [3](#0-2) 

`reward_node_provider_helper` only rejects rewards that **exceed** the maximum; it has no lower-bound (zero) guard:

```rust
if reward.amount_e8s > maximum_node_provider_rewards_e8s {
    Err(...)
} else {
    self.mint_reward_to_neuron_or_account(np_principal, reward).await
}
``` [4](#0-3) 

`mint_reward_to_neuron_or_account` then unconditionally calls `transfer_funds(reward.amount_e8s = 0, ...)` — a zero-amount minting transfer — and, in the `RewardToNeuron` branch, creates a neuron with `cached_neuron_stake_e8s = 0`: [5](#0-4) [6](#0-5) 

The ICP ledger's minting path has no zero-amount guard:

```rust
let transfer = if from == minting_acc {
    assert_eq!(fee, Tokens::ZERO, "Fee for minting should be zero");
    assert_ne!(to, minting_acc, "It is illegal to mint to a minting_account");
    Operation::Mint { to, amount }   // amount == 0 accepted
``` [7](#0-6) 

### Impact Explanation

**Ledger conservation / state pollution.** Every monthly reward cycle, for each node provider whose computed `amount_e8s` rounds to zero, the governance canister issues a zero-amount minting transaction to the ICP ledger. This creates a spurious, permanently-recorded block that inflates the ledger's block height and archive without moving any real value. In the `RewardToNeuron` path (reachable via manual `RewardNodeProvider` proposals), a zero-stake neuron with `kyc_verified = true` is permanently added to governance state, consuming heap memory and polluting the neuron store. Neither artifact can be cleaned up without a governance upgrade.

### Likelihood Explanation

The automated monthly reward timer (`mint_monthly_node_provider_rewards`) fires unconditionally every `NODE_PROVIDER_REWARD_PERIOD_SECONDS`. Any node provider registered in governance whose nodes have been removed from the registry, or whose region/type maps to a zero XDR rate, will produce `xdr_permyriad_reward = 0` and trigger the bug on every monthly cycle. This is a normal operational state (e.g., a node provider whose hardware was decommissioned). No privileged access or attacker action is required beyond the provider being registered. [8](#0-7) 

### Recommendation

Add a zero-amount guard in `get_node_provider_reward` before returning `Some(...)`:

```rust
if amount_e8s == 0 {
    return None;
}
```

Alternatively, add the guard in `reward_node_provider_helper` before calling `mint_reward_to_neuron_or_account`:

```rust
if reward.amount_e8s == 0 {
    return Ok(()); // nothing to mint
}
```

This mirrors the fix applied in the ERC-2981 analog: the 1st priority in `getFees()` already checks `if (royaltyAmount > 0)` before using the result; the same guard must be applied before any minting transfer is issued.

### Proof of Concept

1. Register a node provider in NNS governance (via proposal).
2. Ensure the provider has no active nodes in the registry (or nodes with a zero reward rate in the `NodeRewardsTable`).
3. Wait for the automated monthly reward timer to fire (`mint_monthly_node_provider_rewards`).
4. `get_monthly_node_provider_rewards` calls `get_node_provider_reward(np, 0, xdr_permyriad_per_icp)`, which returns `Some(RewardNodeProvider { amount_e8s: 0, reward_mode: RewardToAccount(...) })`.
5. `reward_node_provider_helper` passes the zero-amount reward to `mint_reward_to_neuron_or_account`.
6. `transfer_funds(0, 0, None, to_account, now)` is called on the ICP ledger.
7. The ICP ledger records a zero-amount `Mint` block, permanently polluting the ledger archive.
8. For manual `RewardNodeProvider` proposals using `RewardToNeuron`, a zero-stake neuron is additionally created in governance state.

### Citations

**File:** rs/nns/governance/src/governance.rs (L3853-3895)
```rust
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
```

**File:** rs/nns/governance/src/governance.rs (L3897-3928)
```rust
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
```

**File:** rs/nns/governance/src/governance.rs (L3948-3964)
```rust
                } else {
                    // Check that the amount to distribute is not above
                    // than the maximum set in network economics.
                    let maximum_node_provider_rewards_e8s =
                        self.economics().maximum_node_provider_rewards_e8s;
                    if reward.amount_e8s > maximum_node_provider_rewards_e8s {
                        Err(GovernanceError::new_with_message(
                            ErrorType::PreconditionFailed,
                            format!(
                                "Proposed reward {} greater than maximum {}",
                                reward.amount_e8s, maximum_node_provider_rewards_e8s
                            ),
                        ))
                    } else {
                        self.mint_reward_to_neuron_or_account(np_principal, reward)
                            .await
                    }
```

**File:** rs/nns/governance/src/governance.rs (L4040-4088)
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
```

**File:** rs/nns/governance/src/governance.rs (L7684-7693)
```rust
        for np in &self.heap_data.node_providers {
            if let Some(np_id) = &np.id {
                let xdr_permyriad_reward = *rewards_per_node_provider.get(np_id).unwrap_or(&0);

                if let Some(reward_node_provider) =
                    get_node_provider_reward(np, xdr_permyriad_reward, xdr_permyriad_per_icp)
                {
                    rewards.push(reward_node_provider);
                }
            }
```

**File:** rs/nns/governance/src/governance.rs (L7753-7762)
```rust
        for np in &self.heap_data.node_providers {
            if let Some(np_id) = &np.id {
                let xdr_permyriad_reward = *reg_rewards.get(np_id).unwrap_or(&0);

                if let Some(reward_node_provider) =
                    get_node_provider_reward(np, xdr_permyriad_reward, xdr_permyriad_per_icp)
                {
                    rewards.push(reward_node_provider);
                }
            }
```

**File:** rs/nns/governance/src/governance.rs (L8248-8271)
```rust
pub fn get_node_provider_reward(
    np: &NodeProvider,
    xdr_permyriad_reward: u64,
    xdr_permyriad_per_icp: u64,
) -> Option<RewardNodeProvider> {
    if let Some(np_id) = np.id.as_ref() {
        let amount_e8s = ((xdr_permyriad_reward as u128 * TOKEN_SUBDIVIDABLE_BY as u128)
            / xdr_permyriad_per_icp as u128) as u64;

        let to_account = Some(if let Some(account) = &np.reward_account {
            account.clone()
        } else {
            AccountIdentifier::from(*np_id).into()
        });

        Some(RewardNodeProvider {
            node_provider: Some(np.clone()),
            amount_e8s,
            reward_mode: Some(RewardMode::RewardToAccount(RewardToAccount { to_account })),
        })
    } else {
        None
    }
}
```

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L213-231)
```rust
    let transfer = if from == minting_acc {
        assert_eq!(fee, Tokens::ZERO, "Fee for minting should be zero");
        assert_ne!(
            to, minting_acc,
            "It is illegal to mint to a minting_account"
        );
        Operation::Mint { to, amount }
    } else if to == minting_acc {
        assert_eq!(fee, Tokens::ZERO, "Fee for burning should be zero");
        let balance = LEDGER.read().unwrap().balances().account_balance(&from);
        let min_burn_amount = LEDGER.read().unwrap().transfer_fee.min(balance);
        if amount < min_burn_amount {
            panic!("Burns lower than {min_burn_amount} are not allowed");
        }
        Operation::Burn {
            from,
            amount,
            spender: None,
        }
```
