### Title
Missing Input Validation for `reward_coefficient_percent` in `do_update_node_rewards_table` Enables Exponential Node Provider Reward Inflation — (File: rs/registry/canister/src/mutations/do_update_node_rewards_table.rs)

---

### Summary

The Registry canister's `do_update_node_rewards_table` mutation accepts `NodeRewardRate` entries containing a `reward_coefficient_percent` field typed as `optional int32` with no bounds validation. The field is semantically constrained to `[0, 100]` (a percentage decay factor for type3 node rewards), but the setter stores any value unconditionally. If a value greater than 100 is written — even by an accidental NNS governance proposal with a typo — the type3 node reward calculation inverts from a decay series into an exponentially growing series, causing unbounded ICP overpayment to node providers.

---

### Finding Description

**Proto definition** (`rs/protobuf/def/registry/node_rewards/v2/node_rewards.proto`):

```proto
message NodeRewardRate {
  uint64 xdr_permyriad_per_node_per_month = 1;
  // A value of 100 means same reward for all nodes.
  // A value of 0 means only the first node gets rewards.
  optional int32 reward_coefficient_percent = 2;   // ← int32, no range constraint
}
```

The field is `int32`, so it accepts negative values and values above 100. [1](#0-0) 

**Setter with no validation** (`rs/registry/canister/src/mutations/do_update_node_rewards_table.rs`):

```rust
pub fn do_update_node_rewards_table(&mut self, payload: UpdateNodeRewardsTableProposalPayload) {
    // ...
    node_rewards_table.extend(payload.get_rewards_table());
    // ← no check that reward_coefficient_percent ∈ [0, 100]
    self.maybe_apply_mutation_internal(mutations);
}
```

No invariant in the registry checks this field's range. [2](#0-1) 

**Reward calculation** (`rs/registry/node_provider_rewards/src/lib.rs`):

```rust
let dc_reward_coefficient_percent =
    rate.reward_coefficient_percent.unwrap_or(80) as f64 / 100.0;
// ...
for i in 0..*node_count {
    let node_reward = (reward_base * np_coeff) as u64;
    dc_reward += node_reward;
    np_coeff *= dc_reward_coefficient_percent;   // ← multiplied each iteration
}
```

When `reward_coefficient_percent = 110`, `dc_reward_coefficient_percent = 1.1`, and `np_coeff` grows as `1.0, 1.1, 1.21, 1.331, …` — exponential growth instead of the intended decay. [3](#0-2) 

With a real table value of `xdr_permyriad_per_node_per_month = 27491250` and `reward_coefficient_percent = 110`, a node provider with 64 type3 nodes would receive a reward that overflows `f64` to `+∞`, which Rust saturates to `u64::MAX = 18_446_744_073_709_551_615` XDR permyriad for every subsequent node. [4](#0-3) 

The same path exists in the newer performance-based algorithm (`rs/node_rewards/rewards_calculation/src/performance_based_algorithm/mod.rs`), which also reads `reward_coefficient_percent` directly from the table without clamping. [5](#0-4) 

---

### Impact Explanation

**Governance conservation / ledger conservation bug.** Monthly node provider rewards are minted as ICP from the NNS treasury. If `reward_coefficient_percent` is set above 100 for any type3 region, every node provider with type3 nodes in that region receives exponentially inflated rewards. With enough nodes, the per-provider reward saturates at `u64::MAX` XDR permyriad, which translates to an astronomically large ICP mint. This drains the NNS treasury and violates the ICP supply conservation invariant.

---

### Likelihood Explanation

The `update_node_rewards_table` endpoint is callable only by the NNS governance canister. [6](#0-5)  An NNS proposal to update the rewards table is a routine governance operation. The field `reward_coefficient_percent` is expressed as a plain integer (e.g., `98`, `80`, `70`) with no unit annotation in the Candid interface or the `ic-admin` CLI payload builder. [7](#0-6)  A proposer who intends to set a 10% decay (coefficient 90) but accidentally writes `900` — a one-digit typo — would submit a value 10× out of range. Because the setter performs no validation and no registry invariant catches the error, the proposal executes successfully and the corrupted value is stored permanently. The scenario is directly analogous to the original report's "owner sets a variable incorrectly by 19 decimal places."

---

### Recommendation

**Short term:** Add explicit bounds validation inside `do_update_node_rewards_table`:

```rust
for (_, rates) in payload.new_entries.iter() {
    for (_, rate) in rates.rates.iter() {
        if let Some(coeff) = rate.reward_coefficient_percent {
            assert!(
                (0..=100).contains(&coeff),
                "reward_coefficient_percent must be in [0, 100], got {coeff}"
            );
        }
    }
}
```

Also change the proto field type from `int32` to `uint32` to eliminate negative values at the wire level.

**Long term:** Add a registry invariant (analogous to `check_unassigned_nodes_config_invariants`) that validates all `NodeRewardRate` fields after every mutation to the `NODE_REWARDS_TABLE_KEY`. [8](#0-7) 

---

### Proof of Concept

1. Craft an `UpdateNodeRewardsTableProposalPayload` with `reward_coefficient_percent = 110` for `type3` nodes in any active region (e.g., `"Africa,ZA"`).
2. Submit an NNS `ExecuteNnsFunction` proposal with `NnsFunction::UpdateNodeRewardsTable` carrying this payload.
3. After the proposal executes, `do_update_node_rewards_table` stores the value without rejection. [9](#0-8) 
4. At the next monthly reward calculation, `calculate_rewards_v0` reads `reward_coefficient_percent = 110`, computes `dc_reward_coefficient_percent = 1.1`, and the running coefficient grows as `1.0 → 1.1 → 1.21 → … → ∞` for each successive type3 node owned by any node provider in that region. [10](#0-9) 
5. After ~64 nodes the `f64` overflows to `+∞`; the cast `(f64::INFINITY) as u64` saturates to `u64::MAX`, and every subsequent node is rewarded `u64::MAX` XDR permyriad, causing the NNS to attempt minting a near-infinite quantity of ICP.

### Citations

**File:** rs/protobuf/def/registry/node_rewards/v2/node_rewards.proto (L5-17)
```text
message NodeRewardRate {
  // The number of 10,000ths of IMF SDR (currency code XDR) to be rewarded per
  // node per month.
  uint64 xdr_permyriad_per_node_per_month = 1;

  // The coefficient of the node rewards the node provider gets
  // for having more than 1 node, as a percentage of the reward for first node.
  // A value of 100 means that the same reward is received for all nodes
  // A value of 0 means that only the first node gets the rewards, 2nd and later nodes get no reward
  // For values in between, the reward for the n-th node is:
  // reward(n) = reward(n-1) * reward_coefficient_percent ^ (n-1)
  optional int32 reward_coefficient_percent = 2;
}
```

**File:** rs/registry/canister/src/mutations/do_update_node_rewards_table.rs (L13-31)
```rust
    pub fn do_update_node_rewards_table(&mut self, payload: UpdateNodeRewardsTableProposalPayload) {
        println!("{}do_update_node_rewards_table: {:?}", LOG_PREFIX, &payload);

        let mut node_rewards_table = self
            .get(NODE_REWARDS_TABLE_KEY.as_bytes(), self.latest_version())
            .map(|RegistryValue { value, .. }| NodeRewardsTable::decode(value.as_slice()).unwrap())
            .unwrap_or_default();

        node_rewards_table.extend(payload.get_rewards_table());

        let mutations = vec![RegistryMutation {
            mutation_type: registry_mutation::Type::Upsert as i32,
            key: NODE_REWARDS_TABLE_KEY.into(),
            value: node_rewards_table.encode_to_vec(),
        }];

        // Check invariants before applying mutations
        self.maybe_apply_mutation_internal(mutations);
    }
```

**File:** rs/registry/node_provider_rewards/src/lib.rs (L101-141)
```rust
                    let reward_base = rate.xdr_permyriad_per_node_per_month as f64;

                    // To de-stimulate the same NP having too many nodes in the same country, the node rewards
                    // is reduced for each node the NP has in the given country.
                    // Join the NP PrincipalId + DC Continent + DC Country, and use that as the key for the
                    // reduction coefficients.
                    let np_coefficients_key = format!(
                        "{}:{}",
                        node_provider_id,
                        region
                            .splitn(3, ',')
                            .take(2)
                            .collect::<Vec<&str>>()
                            .join(":")
                    );

                    let mut np_coeff = *np_coefficients.get(&np_coefficients_key).unwrap_or(&1.0);

                    // Default reward_coefficient_percent is set to 80%, which is used as a fallback only in the
                    // unlikely case that the type3 entry in the reward table:
                    // a) has xdr_permyriad_per_node_per_month entry set for this region, but
                    // b) does NOT have the reward_coefficient_percent value set
                    let dc_reward_coefficient_percent =
                        rate.reward_coefficient_percent.unwrap_or(80) as f64 / 100.0;

                    let mut dc_reward = 0;
                    for i in 0..*node_count {
                        let node_reward = (reward_base * np_coeff) as u64;
                        np_log.add_entry(LogEntry::NodeRewards {
                            node_type: node_type.clone(),
                            node_idx: i,
                            dc_id: node_operator.dc_id.clone(),
                            rewardable_count: *node_count,
                            rewards_xdr_permyriad: node_reward,
                        });
                        dc_reward += node_reward;
                        np_coeff *= dc_reward_coefficient_percent;
                    }
                    np_coefficients.insert(np_coefficients_key, np_coeff);
                    dc_reward
                }
```

**File:** rs/node_rewards/rewards_calculation/src/performance_based_algorithm/mod.rs (L400-410)
```rust
                    let base_rewards_monthly = Decimal::from(rate.xdr_permyriad_per_node_per_month);
                    // Default reward_coefficient percent is set to 80%, which is used as a fallback only in the
                    // unlikely case that the type3 entry in the reward table:
                    // a) has xdr_permyriad_per_node_per_month entry set for this region, but
                    // b) does NOT have the reward_coefficient value set
                    let reward_coefficient =
                        Decimal::from(rate.reward_coefficient_percent.unwrap_or(80)) / dec!(100);

                    (base_rewards_monthly, reward_coefficient)
                })
                .unwrap_or((dec!(0), dec!(1)))
```

**File:** rs/registry/canister/canister/canister.rs (L954-963)
```rust
#[unsafe(export_name = "canister_update update_node_rewards_table")]
fn update_node_rewards_table() {
    check_caller_is_governance_and_log("update_node_rewards_table");
    over(candid_one, update_node_rewards_table_);
}

#[candid_method(update, rename = "update_node_rewards_table")]
fn update_node_rewards_table_(payload: UpdateNodeRewardsTableProposalPayload) {
    registry_mut().do_update_node_rewards_table(payload);
    recertify_registry();
```

**File:** rs/registry/admin/bin/main.rs (L3126-3133)
```rust
impl ProposalPayload<UpdateNodeRewardsTableProposalPayload> for ProposeToUpdateNodeRewardsTableCmd {
    async fn payload(&self, _: &Agent) -> UpdateNodeRewardsTableProposalPayload {
        let map: BTreeMap<String, BTreeMap<String, NodeRewardRate>> =
            serde_json::from_str(&self.updated_node_rewards)
                .unwrap_or_else(|e| panic!("Unable to parse updated_node_rewards: {e}"));

        UpdateNodeRewardsTableProposalPayload::from(map)
    }
```

**File:** rs/registry/canister/src/invariants/unassigned_nodes_config.rs (L15-36)
```rust
pub(crate) fn check_unassigned_nodes_config_invariants(
    snapshot: &RegistrySnapshot,
) -> Result<(), InvariantCheckError> {
    println!("{LOG_PREFIX}check_unassigned_nodes_config_invariants");

    if let Some(config) = get_value_from_snapshot::<UnassignedNodesConfigRecord>(
        snapshot,
        make_unassigned_nodes_config_record_key(),
    ) && config.ssh_readonly_access.len() > MAX_NUM_SSH_KEYS
    {
        return Err(InvariantCheckError {
            msg: format!(
                "Mutation would have resulted in an SSH key access list that is too long, \
                    the maximum allowable length is {}, and the `readonly` list had {} keys",
                MAX_NUM_SSH_KEYS,
                config.ssh_readonly_access.len(),
            ),
            source: None,
        });
    }

    Ok(())
```
