### Title
`ValidRemoveNodeProvider::execute()` Removes Node Provider Without Checking Outstanding Reward Entitlements — (File: `rs/nns/governance/src/proposals/add_or_remove_node_provider.rs`)

---

### Summary

The NNS governance canister's `ValidRemoveNodeProvider::execute()` removes a node provider from `heap_data.node_providers` without verifying whether the provider has accrued but not-yet-distributed monthly ICP rewards. Because the monthly reward distribution loop iterates exclusively over `heap_data.node_providers`, a node provider removed mid-cycle permanently loses all ICP rewards earned during that period, even though the registry's `NodeOperatorRecord.rewardable_nodes` still reflects their contribution.

---

### Finding Description

`ValidRemoveNodeProvider::execute()` performs a bare list removal with no reward-state check:

```rust
pub fn execute(&self, node_providers: &mut Vec<NodeProvider>) -> Result<(), GovernanceError> {
    let existing_node_provider_position =
        self.find_existing_node_provider_position(node_providers)?;
    node_providers.remove(existing_node_provider_position);   // ← no reward check
    Ok(())
}
``` [1](#0-0) 

This function is invoked directly from the proposal execution dispatcher:

```rust
ValidProposalAction::AddOrRemoveNodeProvider(add_or_remove_node_provider) => {
    let result =
        add_or_remove_node_provider.execute(&mut self.heap_data.node_providers);
    self.set_proposal_execution_status::<()>(pid, result.map(|()| vec![]));
}
``` [2](#0-1) 

The monthly reward distribution (both the legacy and performance-based paths) iterates **only** over `self.heap_data.node_providers` to decide who receives ICP:

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
}
``` [3](#0-2) 

The same pattern exists in `get_monthly_node_provider_rewards()`: [4](#0-3) 

The registry's `NodeOperatorRecord.rewardable_nodes` field — which the reward calculation uses to compute XDR amounts per provider — is **not** cleared when a node provider is removed from governance's list. The registry still holds the provider's contribution data, but governance will never distribute the corresponding ICP because the provider is no longer in `heap_data.node_providers`. [5](#0-4) 

The `validate()` and `execute()` preconditions for `ValidRemoveNodeProvider` check only that the provider exists in the list — no reward-state check is performed at either validation or execution time: [6](#0-5) 

---

### Impact Explanation

**Impact: Medium**

A node provider who has operated nodes for part of a monthly reward cycle (up to ~30 days) and is then removed via a governance proposal before `mint_monthly_node_provider_rewards()` fires loses all ICP rewards accrued during that period. The ICP that should have been minted to the provider is simply never minted — it is not redistributed, not burned, and not recoverable. This is a ledger conservation violation: the registry records a reward obligation that governance silently discards. [7](#0-6) 

---

### Likelihood Explanation

**Likelihood: Medium**

`AddOrRemoveNodeProvider` proposals are a normal, recurring governance action on the NNS. The monthly reward period is approximately 30 days (`NODE_PROVIDER_REWARD_PERIOD_SECONDS`). Any removal proposal that executes in the window between the last reward distribution and the next one causes the provider to forfeit that period's rewards. Voters approving the removal proposal have no on-chain signal that rewards are pending; the governance UI and proposal payload carry no such warning. The scenario does not require a malicious majority — an honest majority acting on a legitimate removal request is sufficient to trigger the loss. [8](#0-7) 

---

### Recommendation

Before removing a node provider from `heap_data.node_providers`, `ValidRemoveNodeProvider::execute()` (or its caller in the governance dispatcher) should:

1. Query the registry for the provider's current `rewardable_nodes` across all associated `NodeOperatorRecord`s.
2. If any rewardable nodes exist (i.e., the provider has an outstanding reward entitlement for the current cycle), either:
   - **Reject** the removal with a `PreconditionFailed` error until rewards are distributed, or
   - **Trigger an immediate reward distribution** for the provider before removing them, analogous to how `mint_monthly_node_provider_rewards` works.

This mirrors the pattern already used in `do_remove_node_operators`, which filters out operators that still have active node records before allowing removal: [9](#0-8) 

---

### Proof of Concept

1. Node provider `NP_A` is registered in `heap_data.node_providers` and has been operating nodes for 25 days of the current 30-day reward cycle. The registry's `NodeOperatorRecord` for `NP_A`'s operator reflects `rewardable_nodes: {"type1": 5}`.

2. A governance proposal `AddOrRemoveNodeProvider { change: ToRemove(NP_A) }` is submitted and passes.

3. `ValidRemoveNodeProvider::execute()` removes `NP_A` from `heap_data.node_providers` with no reward check.

4. Five days later, `mint_monthly_node_provider_rewards()` fires. It calls `get_node_providers_rewards()` (or `get_monthly_node_provider_rewards()`), which iterates over `self.heap_data.node_providers`. `NP_A` is absent from this list.

5. The registry still returns a non-zero XDR reward for `NP_A`'s principal (because `rewardable_nodes` is still set), but governance never looks it up — `NP_A` receives zero ICP for 25 days of node operation. [1](#0-0) [3](#0-2)

### Citations

**File:** rs/nns/governance/src/proposals/add_or_remove_node_provider.rs (L201-211)
```rust
impl ValidRemoveNodeProvider {
    pub fn validate(&self, node_providers: &[NodeProvider]) -> Result<(), GovernanceError> {
        self.find_existing_node_provider_position(node_providers)?;
        Ok(())
    }

    pub fn execute(&self, node_providers: &mut Vec<NodeProvider>) -> Result<(), GovernanceError> {
        let existing_node_provider_position =
            self.find_existing_node_provider_position(node_providers)?;
        node_providers.remove(existing_node_provider_position);
        Ok(())
```

**File:** rs/nns/governance/src/governance.rs (L4023-4033)
```rust
    /// Return `true` if `NODE_PROVIDER_REWARD_PERIOD_SECONDS` has passed since the last monthly
    /// node provider reward event
    fn is_time_to_mint_monthly_node_provider_rewards(&self) -> bool {
        match &self.heap_data.most_recent_monthly_node_provider_rewards {
            None => false,
            Some(recent_rewards) => {
                self.env.now().saturating_sub(recent_rewards.timestamp)
                    >= NODE_PROVIDER_REWARD_PERIOD_SECONDS
            }
        }
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

**File:** rs/nns/governance/src/governance.rs (L4230-4234)
```rust
            ValidProposalAction::AddOrRemoveNodeProvider(add_or_remove_node_provider) => {
                let result =
                    add_or_remove_node_provider.execute(&mut self.heap_data.node_providers);
                self.set_proposal_execution_status::<()>(pid, result.map(|()| vec![]));
            }
```

**File:** rs/nns/governance/src/governance.rs (L7684-7694)
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
        }
```

**File:** rs/nns/governance/src/governance.rs (L7753-7763)
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
        }
```

**File:** rs/registry/canister/src/get_node_providers_monthly_xdr_rewards.rs (L35-59)
```rust
        let node_operators = get_key_family_iter_at_version::<NodeOperatorRecord>(
            self,
            NODE_OPERATOR_RECORD_KEY_PREFIX,
            version,
        )
        .collect::<Vec<_>>();

        let data_centers = get_key_family_iter_at_version::<DataCenterRecord>(
            self,
            DATA_CENTER_KEY_PREFIX,
            version,
        )
        .collect::<BTreeMap<String, DataCenterRecord>>();

        let reward_values = calculate_rewards_v0(&rewards_table, &node_operators, &data_centers)?;

        rewards.rewards = reward_values
            .rewards_per_node_provider
            .into_iter()
            .map(|(k, v)| (k.to_string(), v))
            .collect();

        rewards.registry_version = Some(version);

        Ok(rewards)
```

**File:** rs/registry/canister/src/mutations/do_remove_node_operators.rs (L51-68)
```rust
    /// Takes a set of node operators and removes the node operators for which
    /// there exist at least one node record that is managed by the node
    /// operator.
    ///
    /// In other words, this retains only "empty" node operators, i.e. those
    /// that have ZERO nodes.
    fn filter_out_node_operators_that_have_nodes(&self, node_operators: &mut Vec<PrincipalId>) {
        if node_operators.is_empty() {
            return;
        }

        // This implementation is inefficient, because it does a full scan of all nodes.
        for (_key, node_record) in get_key_family_iter::<NodeRecord>(self, NODE_RECORD_KEY_PREFIX) {
            // Throw out node operators that operate the node (that this loop is currently considering).
            node_operators
                .retain(|node_operator| node_operator.to_vec() != node_record.node_operator_id);
        }
    }
```
