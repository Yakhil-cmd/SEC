### Title
Zero-Amount Node Provider Reward Minting Without Guard - (File: rs/nns/governance/src/governance.rs)

### Summary
`get_node_provider_reward` unconditionally returns a `RewardNodeProvider` with `amount_e8s = 0` when a node provider has no XDR rewards. This zero-amount reward is passed directly to `mint_reward_to_neuron_or_account` without any guard, causing a zero-amount minting transfer to be attempted against the ICP ledger. In the `RewardToNeuron` path, this additionally creates a zero-stake neuron in governance state.

### Finding Description
`get_node_provider_reward` at line 8248 computes `amount_e8s` via integer division:

```rust
let amount_e8s = ((xdr_permyriad_reward as u128 * TOKEN_SUBDIVIDABLE_BY as u128)
    / xdr_permyriad_per_icp as u128) as u64;
```

When `xdr_permyriad_reward = 0` (node provider has no nodes in the registry, or all nodes are offline and produce zero performance metrics), `amount_e8s` evaluates to `0`. The function still returns `Some(RewardNodeProvider { amount_e8s: 0, ... })` because the only guard is `if let Some(np_id) = np.id.as_ref()`. There is no `amount_e8s == 0` early-return. [1](#0-0) 

This zero-amount `RewardNodeProvider` is pushed into the rewards list in both `get_monthly_node_provider_rewards` and `get_node_providers_rewards`:

```rust
let xdr_permyriad_reward = *reg_rewards.get(np_id).unwrap_or(&0);
if let Some(reward_node_provider) =
    get_node_provider_reward(np, xdr_permyriad_reward, xdr_permyriad_per_icp)
{
    rewards.push(reward_node_provider);  // pushed even when amount_e8s == 0
}
``` [2](#0-1) 

`reward_node_provider_helper` only checks the upper bound (`amount_e8s > maximum`), not the lower bound (`amount_e8s == 0`), before calling `mint_reward_to_neuron_or_account`: [3](#0-2) 

`mint_reward_to_neuron_or_account` then calls `transfer_funds` with `reward.amount_e8s = 0` in both the `RewardToAccount` and `RewardToNeuron` branches, with no zero-amount guard: [4](#0-3) 

In the `RewardToNeuron` branch, if the zero-amount mint transfer succeeds, a neuron is created with `cached_neuron_stake_e8s = 0`: [5](#0-4) 

Contrast this with the SNS governance's `distribute_rewards`, which explicitly guards against zero before proceeding: [6](#0-5) 

And the NNS governance's own `calculate_voting_rewards`, which guards against near-zero `total_voting_rights`: [7](#0-6) 

No equivalent guard exists for zero `amount_e8s` in the node provider reward path.

### Impact Explanation
1. **Wasted cycles**: Every monthly reward cycle, a zero-amount minting call is made to the ICP ledger for each node provider with no XDR rewards. The ICP ledger's `send` path accepts zero-amount mints (confirmed by the `balances_remove_accounts_with_zero_balance` test at line 105–116 of `rs/ledger_suite/icp/ledger/src/tests.rs`), so the call succeeds but transfers nothing. Cycles are consumed for a no-op cross-canister call. [8](#0-7) 

2. **Zero-stake neuron creation** (manual `RewardNodeProvider` proposal path): If a governance proposal specifies `RewardToNeuron` with `amount_e8s = 0`, a neuron with zero stake is created and persisted in the neuron store, polluting governance state with a ghost neuron that can accumulate maturity and vote.

3. **Silent reward skip with timestamp advance**: `mint_monthly_node_provider_rewards` ignores the return value of `reward_node_providers` (`let _ = ...`) and unconditionally calls `update_most_recent_monthly_node_provider_rewards`. If a future ledger version rejects zero-amount mints, the failure would be silently swallowed while the monthly timestamp advances, permanently skipping that node provider's reward for the period. [9](#0-8) 

### Likelihood Explanation
Node providers with zero XDR rewards are a realistic steady-state condition: a registered node provider whose nodes are all offline, removed from subnets, or not yet assigned will have `xdr_permyriad_reward = 0` from the registry or node rewards canister. The monthly automated reward task (`mint_monthly_node_provider_rewards`) runs unconditionally for all registered node providers, so any node provider in this state triggers the zero-amount path every reward period without any additional action. [10](#0-9) 

### Recommendation
Add a zero-amount guard in `get_node_provider_reward` to return `None` when `amount_e8s == 0`, consistent with how the function already returns `None` when `np.id` is absent:

```rust
if amount_e8s == 0 {
    return None;
}
```

Alternatively, add the guard in `mint_reward_to_neuron_or_account` or `reward_node_provider_helper` to skip zero-amount rewards with a log message, mirroring the pattern used in `calculate_voting_rewards` and SNS `distribute_rewards`.

### Proof of Concept
1. A node provider `NP` is registered in NNS governance (via a legitimate governance proposal).
2. All of `NP`'s nodes go offline; the node rewards canister reports zero XDR rewards for `NP`.
3. The monthly reward timer fires; `mint_monthly_node_provider_rewards` calls `get_node_providers_rewards` (or `get_monthly_node_provider_rewards`).
4. `get_node_provider_reward(np, 0, xdr_permyriad_per_icp)` returns `Some(RewardNodeProvider { amount_e8s: 0, ... })`.
5. `reward_node_provider_helper` passes the zero-amount reward to `mint_reward_to_neuron_or_account`.
6. `transfer_funds(0, 0, None, to_account, now)` is called — a zero-amount mint that succeeds but transfers nothing, consuming cycles.
7. `update_most_recent_monthly_node_provider_rewards` advances the timestamp regardless.

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

**File:** rs/nns/governance/src/governance.rs (L4067-4076)
```rust
        let monthly_node_provider_rewards = if are_performance_based_rewards_enabled() {
            self.get_node_providers_rewards().await?
        } else {
            self.get_monthly_node_provider_rewards().await?
        };

        let _ = self
            .reward_node_providers(&monthly_node_provider_rewards.rewards)
            .await;
        self.update_most_recent_monthly_node_provider_rewards(monthly_node_provider_rewards);
```

**File:** rs/nns/governance/src/governance.rs (L6712-6719)
```rust
        let reward_distribution = if total_voting_rights < 0.001 {
            println!(
                "{}WARNING: total_voting_rights == {}, even though considered_proposals \
                 is nonempty (see earlier log). Therefore, we skip incrementing maturity \
                 to avoid dividing by zero (or super small number).",
                LOG_PREFIX, total_voting_rights,
            );
            None
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

**File:** rs/sns/governance/src/governance.rs (L5946-5953)
```rust
        if total_reward_shares == dec!(0) {
            log!(
                ERROR,
                "Warning: total_reward_shares is 0. Therefore, we skip increasing \
                 neuron maturity. neuron_id_to_reward_shares: {:#?}",
                neuron_id_to_reward_shares,
            );
        } else {
```

**File:** rs/ledger_suite/icp/ledger/src/tests.rs (L105-116)
```rust
    apply_operation(
        &mut ctx,
        &Operation::Mint {
            to: canister,
            amount: Tokens::from_e8s(0),
        },
        now,
    )
    .unwrap();

    // No new account should have been created
    assert_eq!(balances_len(), 1);
```
