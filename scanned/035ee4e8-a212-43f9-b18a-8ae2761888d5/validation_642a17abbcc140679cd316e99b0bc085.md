### Title
Unbounded `rewards` Array in `RewardNodeProviders` NNS Proposal Enables Approved Proposal Execution Failure — (File: `rs/nns/governance/src/governance.rs`)

### Summary
The NNS Governance canister's `RewardNodeProviders` proposal action accepts an unbounded `rewards` array with no length validation at proposal submission time. When an approved proposal is executed, `reward_node_providers()` iterates over all rewards making async ledger calls for each entry. A proposal with a sufficiently large `rewards` array could cause the governance canister to spend excessive resources during execution, spanning thousands of message segments, and could cause the approved proposal to fail or the governance canister to be unresponsive for an extended period.

### Finding Description
The `RewardNodeProviders` struct contains a `rewards: Vec<RewardNodeProvider>` field with no length cap. [1](#0-0) 

During proposal validation, `validate_proposal_action()` explicitly returns `Ok(())` for `RewardNodeProviders` without inspecting the `rewards` array length at all: [2](#0-1) 

When the approved proposal is executed, `reward_node_providers_from_proposal()` calls `reward_node_providers()`, which iterates over the unbounded array: [3](#0-2) 

Each iteration calls `reward_node_provider_helper()`, which first performs an O(N) linear scan over all registered node providers to verify existence, then makes an async call to the ICP ledger to mint rewards: [4](#0-3) 

With a large `rewards` array (up to ~5,000–10,000 entries within the 2 MiB ingress message size limit), the execution spans thousands of message segments, each making an async ledger call. Contrast this with the `ApproveGenesisKyc` action, which received an explicit 1,000-neuron cap at execution time (Proposal 135702): [5](#0-4) 

No analogous cap exists for `RewardNodeProviders.rewards`.

The proto definition confirms the field is `repeated` with no constraint: [6](#0-5) 

### Impact Explanation
An approved `RewardNodeProviders` proposal with a large `rewards` array causes:

1. **Execution spanning thousands of message segments** — each reward entry triggers an async ledger call, resetting the instruction counter but consuming real wall-clock time and canister resources.
2. **Governance canister unavailability** — while iterating through thousands of rewards, the governance canister is occupied and cannot process other ingress messages or timers promptly.
3. **Proposal execution failure** — if any ledger call fails (e.g., ledger busy, cycles exhausted), `reward_node_providers()` records the error but continues; the final status is set to failed, meaning an approved proposal permanently fails to execute.
4. **O(M × N) instruction cost per segment** — each segment performs a linear scan over all registered node providers (`self.heap_data.node_providers`) before the await, so with M rewards and N node providers, total pre-await work is O(M × N) spread across M segments.

The net effect is a direct analog to the reported vulnerability: an adopted governance proposal that cannot be successfully executed.

### Likelihood Explanation
- Any NNS neuron holder with sufficient dissolve delay can submit a `RewardNodeProviders` proposal; the proposal fee is 10 ICP (reject cost).
- The proposal type is legitimate and routinely used; a large `rewards` array would not be obviously malicious to voters.
- There is no on-chain enforcement preventing submission of a proposal with thousands of reward entries.
- The `use_registry_derived_rewards = false` path (which uses the explicit `rewards` array) is the direct attack vector; the `use_registry_derived_rewards = true` path is bounded by the actual number of registered node providers.

### Recommendation
Add a hard cap on `rewards.len()` enforced at proposal submission time inside `validate_proposal_action()` for the `ValidProposalAction::RewardNodeProviders` arm, analogous to the `APPROVE_GENESIS_KYC_MAX_NEURONS` cap added for `ApproveGenesisKyc`. A reasonable limit would be the maximum number of registered node providers (currently a few hundred on mainnet).

### Proof of Concept
1. A neuron holder submits a `RewardNodeProviders` proposal with `use_registry_derived_rewards = false` and a `rewards` array containing ~5,000 entries, each referencing a valid registered node provider principal.
2. The proposal passes the NNS governance vote (it appears legitimate — it rewards known node providers).
3. `reward_node_providers_from_proposal()` is invoked; it calls `reward_node_providers()` which enters a loop over 5,000 entries.
4. For each entry, `reward_node_provider_helper()` scans `heap_data.node_providers` (O(N)) then awaits a ledger mint call — 5,000 async round-trips to the ICP ledger.
5. The governance canister is occupied for the duration; other governance operations (timers, heartbeats, ingress) are delayed.
6. If any ledger call fails (e.g., ledger is temporarily unavailable), the proposal is marked as failed — an approved proposal that cannot execute.

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

**File:** rs/nns/governance/src/governance.rs (L4895-4898)
```rust
            ValidProposalAction::ApproveGenesisKyc(_)
            | ValidProposalAction::RewardNodeProvider(_)
            | ValidProposalAction::RewardNodeProviders(_)
            | ValidProposalAction::FulfillSubnetRentalRequest(_) => Ok(()),
```

**File:** rs/nns/governance/src/neuron_store.rs (L1046-1066)
```rust
    const APPROVE_GENESIS_KYC_MAX_NEURONS: usize = 1000;

    let principal_set: HashSet<PrincipalId> = principals.iter().cloned().collect();
    let neuron_id_to_principal = principal_set
        .into_iter()
        .flat_map(|principal| {
            neuron_store
                .get_neuron_ids_readable_by_caller(principal)
                .into_iter()
                .map(move |neuron_id| (neuron_id, principal))
        })
        .collect::<HashMap<_, _>>();

    if neuron_id_to_principal.len() > APPROVE_GENESIS_KYC_MAX_NEURONS {
        return Err(GovernanceError::new_with_message(
            ErrorType::PreconditionFailed,
            format!(
                "ApproveGenesisKyc can only change the KYC status of up to {APPROVE_GENESIS_KYC_MAX_NEURONS} neurons at a time"
            ),
        ));
    }
```

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L599-605)
```text
message RewardNodeProviders {
  repeated RewardNodeProvider rewards = 1;

  // If true, reward Node Providers with the rewards returned by the Registry's
  // get_node_providers_monthly_xdr_rewards method
  optional bool use_registry_derived_rewards = 2;
}
```
