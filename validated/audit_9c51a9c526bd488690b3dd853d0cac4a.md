### Title
Duplicate Node Provider Entries in `RewardNodeProviders` Proposal Allow Excess ICP Minting - (File: `rs/nns/governance/src/governance.rs`)

---

### Summary

The `reward_node_providers()` function in NNS Governance iterates over the `rewards` list of a `RewardNodeProviders` proposal without checking for duplicate node provider entries. If the same node provider appears more than once in the list, they are rewarded multiple times in a single proposal execution, minting excess ICP beyond what was intended.

---

### Finding Description

The `RewardNodeProviders` proposal type carries a `rewards: Vec<RewardNodeProvider>` field — a plain, unbounded vector with no uniqueness constraint. [1](#0-0) 

When a `RewardNodeProviders` proposal is adopted and executed, `reward_node_providers_from_proposal()` delegates to `reward_node_providers()`: [2](#0-1) 

`reward_node_providers()` iterates over every entry in the slice and calls `reward_node_provider_helper()` for each one, with no deduplication step: [3](#0-2) 

`reward_node_provider_helper()` only validates that the node provider exists in the registry and that the reward amount does not exceed the per-provider cap. It performs no cross-entry uniqueness check: [4](#0-3) 

Consequently, a `rewards` list containing the same `NodeProvider` principal twice will cause `mint_reward_to_neuron_or_account()` to be called twice for that principal, minting double (or more) the intended ICP.

By contrast, the `AddOrRemoveNodeProvider` path does enforce uniqueness — `ValidAddNodeProvider::validate()` explicitly rejects a provider that already exists in the list: [5](#0-4) 

No equivalent guard exists for the `RewardNodeProviders` execution path.

---

### Impact Explanation

**Ledger conservation bug.** Each duplicated entry causes an independent call to the ICP ledger's mint endpoint. The result is that more ICP is minted than the proposal intended, directly violating the token supply invariant. The excess ICP is credited to the node provider's reward account or a newly created neuron, and cannot be clawed back after the fact.

---

### Likelihood Explanation

**Low.** Exploitation requires a `RewardNodeProviders` proposal containing duplicate entries to pass NNS governance voting. This can occur in two ways:

1. **Accidental:** A proposer constructs the `rewards` list with a copy-paste error or tooling bug, and NNS voters approve without auditing every entry for uniqueness.
2. **Deliberate:** A malicious proposer crafts a proposal with a subtle duplicate (e.g., identical principal, different `amount_e8s` or `reward_mode`) hoping reviewers do not notice.

The NNS governance process provides a social check, but there is no on-chain enforcement. The analogous `AddOrRemoveNodeProvider` path shows that the team is aware of the duplicate-entry risk in related flows, making the absence of a guard here a notable inconsistency.

---

### Recommendation

**Short term:** Document that `RewardNodeProviders.rewards` must not contain duplicate node provider principals, and add an explicit check in the NNS proposal review process.

**Long term:** Add a deduplication guard in `reward_node_providers()` (or at proposal submission/validation time) that rejects any `rewards` list containing the same node provider principal more than once, mirroring the guard already present in `ValidAddNodeProvider::validate()`.

---

### Proof of Concept

1. Register node provider `NP_A` via an `AddOrRemoveNodeProvider` proposal.
2. Submit a `RewardNodeProviders` proposal whose `rewards` field contains two entries both referencing `NP_A`:
   ```
   rewards: [
     RewardNodeProvider { node_provider: NP_A, amount_e8s: X, reward_mode: RewardToAccount },
     RewardNodeProvider { node_provider: NP_A, amount_e8s: X, reward_mode: RewardToAccount },
   ]
   ```
3. Once the proposal passes NNS voting, `reward_node_providers_from_proposal()` calls `reward_node_providers()`, which calls `reward_node_provider_helper()` twice for `NP_A`.
4. Each call independently passes the existence check and the per-provider cap check, then calls `mint_reward_to_neuron_or_account()`.
5. `NP_A`'s account receives `2 × X` e8s instead of `X` e8s; the ICP ledger total supply increases by `2 × X` rather than `X`. [3](#0-2) [6](#0-5)

### Citations

**File:** rs/nns/governance/src/gen/ic_nns_governance.pb.v1.rs (L367-374)
```rust
pub struct RewardNodeProviders {
    #[prost(message, repeated, tag = "1")]
    pub rewards: ::prost::alloc::vec::Vec<RewardNodeProvider>,
    /// If true, reward Node Providers with the rewards returned by the Registry's
    /// get_node_providers_monthly_xdr_rewards method
    #[prost(bool, optional, tag = "2")]
    pub use_registry_derived_rewards: ::core::option::Option<bool>,
}
```

**File:** rs/nns/governance/src/governance.rs (L3932-3978)
```rust
    async fn reward_node_provider_helper(
        &mut self,
        reward: &RewardNodeProvider,
    ) -> Result<(), GovernanceError> {
        if let Some(node_provider) = &reward.node_provider {
            if let Some(np_principal) = &node_provider.id {
                if !self
                    .heap_data
                    .node_providers
                    .iter()
                    .any(|np| np.id == node_provider.id)
                {
                    Err(GovernanceError::new_with_message(
                        ErrorType::NotFound,
                        format!("Node provider with id {np_principal} not found."),
                    ))
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
                }
            } else {
                Err(GovernanceError::new_with_message(
                    ErrorType::PreconditionFailed,
                    "Node provider has no ID.",
                ))
            }
        } else {
            Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Proposal was missing the node provider.",
            ))
        }
    }
```

**File:** rs/nns/governance/src/governance.rs (L3986-4006)
```rust
    /// Mint and transfer the specified Node Provider rewards
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

**File:** rs/nns/governance/src/governance.rs (L4008-4021)
```rust
    /// Execute a RewardNodeProviders proposal
    async fn reward_node_providers_from_proposal(
        &mut self,
        pid: u64,
        reward_nps: RewardNodeProviders,
    ) {
        let result = if reward_nps.use_registry_derived_rewards == Some(true) {
            self.mint_monthly_node_provider_rewards().await
        } else {
            self.reward_node_providers(&reward_nps.rewards).await
        };

        self.set_proposal_execution_status::<()>(pid, result.map(|()| vec![]));
    }
```

**File:** rs/nns/governance/src/proposals/add_or_remove_node_provider.rs (L148-161)
```rust
impl ValidAddNodeProvider {
    pub fn validate(&self, node_providers: &[NodeProvider]) -> Result<(), GovernanceError> {
        let already_exists = node_providers
            .iter()
            .any(|node_provider| node_provider.id == Some(self.id));
        if already_exists {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!("NodeProvider with id {} already exists", self.id),
            ));
        }

        Ok(())
    }
```
