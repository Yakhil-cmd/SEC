### Title
Node Provider Reward Fully Rejected Instead of Capped When Exceeding `maximum_node_provider_rewards_e8s` - (File: rs/nns/governance/src/governance.rs)

### Summary

In the NNS Governance canister, when a node provider's automatically calculated monthly reward exceeds `maximum_node_provider_rewards_e8s`, the entire reward is rejected and the node provider receives **zero** ICP, instead of being capped at the maximum. This is a direct analog to the reported gauge-emission revert bug: a dynamic, fluctuating amount is checked against a static cap, and the system rejects rather than fills to the cap.

### Finding Description

The automated monthly node provider reward flow has two separate stages that are inconsistent with each other:

**Stage 1 — Reward Calculation (no cap applied):**

`get_node_provider_reward` computes the ICP reward from XDR metrics without applying `maximum_node_provider_rewards_e8s`:

```rust
let amount_e8s = ((xdr_permyriad_reward as u128 * TOKEN_SUBDIVIDABLE_BY as u128)
    / xdr_permyriad_per_icp as u128) as u64;
``` [1](#0-0) 

**Stage 2 — Reward Distribution (hard reject on cap breach):**

`reward_node_provider_helper` then checks the computed amount against the cap and returns a hard error — the node provider receives nothing:

```rust
if reward.amount_e8s > maximum_node_provider_rewards_e8s {
    Err(GovernanceError::new_with_message(
        ErrorType::PreconditionFailed,
        format!(
            "Proposed reward {} greater than maximum {}",
            reward.amount_e8s, maximum_node_provider_rewards_e8s
        ),
    ))
``` [2](#0-1) 

The automated monthly path calls `reward_node_providers`, which iterates over all rewards and calls `reward_node_provider_helper` for each: [3](#0-2) 

The mainnet value of `maximum_node_provider_rewards_e8s` is **100,000 ICP** (set in `with_mainnet_values()`): [4](#0-3) 

The default test value is 1,000,000 ICP: [5](#0-4) 

The field is documented as "The maximum rewards to be distributed to NodeProviders in a **single distribution event**": [6](#0-5) 

### Impact Explanation

A node provider whose calculated monthly ICP reward exceeds `maximum_node_provider_rewards_e8s` receives **zero** ICP for that month instead of the capped maximum. The reward is permanently lost — there is no retry or rollover mechanism. This is a **ledger conservation bug**: ICP that should have been minted and distributed is silently dropped. The error is only logged, not surfaced to the node provider. [7](#0-6) 

### Likelihood Explanation

The mainnet cap is 100,000 ICP per distribution event. The reward amount is dynamic: it depends on the number of nodes operated, their performance metrics, and the 30-day average XDR/ICP conversion rate. When the ICP price is low (close to the enforced `minimum_icp_xdr_rate`), the ICP equivalent of a large XDR reward is inflated. A large node provider operating hundreds of nodes at a low ICP price could plausibly exceed the cap. The `minimum_icp_xdr_rate` floor (1 XDR = 1 ICP at the minimum) means the ICP equivalent can be at most `xdr_reward / 1`, which for a provider with ~5,000 nodes at ~20 XDR/node/month = 100,000 XDR = 100,000 ICP — exactly at the boundary. Any provider above this scale, or any governance-approved reduction of the cap, triggers the bug. [8](#0-7) 

### Recommendation

In `reward_node_provider_helper`, replace the hard rejection with a cap:

```rust
let amount_e8s = reward.amount_e8s.min(maximum_node_provider_rewards_e8s);
// proceed with amount_e8s instead of reward.amount_e8s
```

Alternatively, apply the cap inside `get_node_provider_reward` so the reward is already bounded before it reaches the distribution stage. [9](#0-8) 

### Proof of Concept

1. NNS governance has `maximum_node_provider_rewards_e8s = 100_000 * E8` (mainnet value).
2. A node provider operates 5,001 nodes, each earning 20 XDR/month = 100,020 XDR total.
3. The 30-day average XDR/ICP rate equals the `minimum_icp_xdr_rate` floor (1 XDR ≈ 1 ICP).
4. `get_node_provider_reward` computes `amount_e8s = 100_020 * E8` — exceeds the 100,000 ICP cap by 20 ICP.
5. `reward_node_provider_helper` evaluates `100_020 * E8 > 100_000 * E8` → `true` → returns `Err(PreconditionFailed)`.
6. `reward_node_providers` logs the error and continues; the node provider receives **0 ICP** instead of the capped **100,000 ICP**.
7. The monthly reward event completes successfully from the canister's perspective; the loss is silent. [10](#0-9) [11](#0-10)

### Citations

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

**File:** rs/nns/governance/src/governance.rs (L3987-4005)
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
```

**File:** rs/nns/governance/src/governance.rs (L7678-7680)
```rust
        let maximum_node_provider_rewards_e8s = self.economics().maximum_node_provider_rewards_e8s;

        let xdr_permyriad_per_icp = max(avg_xdr_permyriad_per_icp, minimum_xdr_permyriad_per_icp);
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

**File:** rs/nns/governance/api/src/lib.rs (L102-107)
```rust
    pub fn with_mainnet_values() -> Self {
        let mut network_economics = Self::with_default_values();
        network_economics.reject_cost_e8s = 25 * E8S_PER_ICP; // 25 ICP
        network_economics.maximum_node_provider_rewards_e8s = 100_000 * E8S_PER_ICP; // 100k ICP
        network_economics
    }
```

**File:** rs/nns/governance/src/network_economics.rs (L26-26)
```rust
            maximum_node_provider_rewards_e8s: 1_000_000 * 100_000_000, // 1M ICP
```

**File:** rs/nns/governance/src/gen/ic_nns_governance.pb.v1.rs (L2073-2076)
```rust
    /// The maximum rewards to be distributed to NodeProviders in a single
    /// distribution event, in e8s.
    #[prost(uint64, tag = "8")]
    pub maximum_node_provider_rewards_e8s: u64,
```
