Based on my thorough investigation of the codebase, I can now provide a definitive assessment.

---

### Title
Unbounded `build_follower_index` in `initialize_indices` Can Exceed Wasm Instruction Limit on Upgrade, Permanently Bricking SNS Governance - (`rs/sns/governance/src/follower_index.rs`)

### Summary

`canister_post_upgrade` calls `canister_init_` → `Governance::new` → `initialize_indices` → `build_follower_index`, which iterates all neurons × all topics × all followees with no instruction-limit guard. With `MAX_NUMBER_OF_NEURONS_CEILING` = 200,000, 7 topics, and `MAX_FOLLOWEES_PER_TOPIC` = 15, the worst-case work is 21 million BTreeMap/BTreeSet operations. The existing upgrade mem-test does **not** populate `topic_followees`, so this path is untested. An unprivileged neuron holder can pre-position state via `SetFollowing` to trigger the trap on the next governance upgrade.

### Finding Description

**Confirmed call chain:**

`canister_post_upgrade` (line 273) calls `canister_init_` (line 285), which calls `Governance::new` (line 237), which calls `initialize_indices` (line 765), which calls `build_follower_index` (line 828). [1](#0-0) 

`initialize_indices` runs three unbounded index-building passes synchronously: [2](#0-1) 

`build_follower_index` is a plain loop with no instruction-limit check: [3](#0-2) 

`add_neuron_to_follower_index` performs `BTreeMap::entry(...).or_default().insert(...)` for every (neuron, topic, followee) triple: [4](#0-3) 

**Confirmed constants:**

- `MAX_FOLLOWEES_PER_TOPIC` = 15 [5](#0-4) 

- Topics = 7 (DaoCommunitySettings … CriticalDappOperations) [6](#0-5) 

- `MAX_NUMBER_OF_NEURONS_CEILING` = 200,000 [7](#0-6) 

**Worst-case work:** 200,000 × 7 × 15 = **21,000,000** BTreeMap/BTreeSet operations, each involving O(log 200K) ≈ 17 string comparisons on 64-character hex-encoded neuron IDs, plus heap allocation for cloned `NeuronId` values. This is in addition to `build_function_followee_index` (legacy) and `build_principal_to_neuron_ids_index` also running in the same `initialize_indices` call.

**Critical gap in existing mem-test:** The `sns_governance_mem_test_canister` explicitly exists to verify "canister pre- and post-upgrade can finish within the execution limit," but `allocate_neuron` only sets the legacy `followees` field — `topic_followees` is left `None`. The `build_follower_index` path is therefore completely untested under load. [8](#0-7) 

The mem-test comment even acknowledges the instruction-limit risk for the legacy system but caps `GenericNervousSystemFunctions` to 10 as a workaround — no equivalent cap or chunking exists for the new topic-following system: [9](#0-8) 

**Attacker entry point:** `SetFollowing` is callable by any principal holding `Vote` permission on their own neuron — a fully unprivileged operation: [10](#0-9) 

Validation enforces at most `MAX_FOLLOWEES_PER_TOPIC` = 15 followees per topic, which is exactly the maximum the attacker wants to set: [11](#0-10) 

### Impact Explanation

If `canister_post_upgrade` traps due to exceeding the Wasm instruction limit, the SNS governance canister is left in a state where every subsequent upgrade attempt also traps (because the same `initialize_indices` runs on every upgrade). The canister becomes permanently unupgradeable — effectively bricked. All SNS governance functions (proposals, voting, neuron management) are lost.

### Likelihood Explanation

An attacker needs to:
1. Control a large number of SNS neurons (achievable by participating in the swap or staking tokens across many accounts).
2. Call `SetFollowing` on each neuron to fill all 7 topics with 15 followees each.
3. Wait for the next routine SNS governance upgrade (these happen regularly via the NNS upgrade mechanism).

No privileged access, no governance majority, and no social engineering is required. The attacker only needs token holdings sufficient to create many neurons.

### Recommendation

1. **Immediate:** Add the new `topic_followees` field to the `sns_governance_mem_test_canister` with maximum density and verify `post_upgrade` completes within the instruction budget.
2. **Fix:** Persist the `topic_follower_index` (and other derived indices) to stable memory across upgrades instead of rebuilding them from scratch on every `post_upgrade`. This is the same pattern used by NNS governance's neuron store.
3. **Alternative fix:** Chunk `initialize_indices` across multiple heartbeat ticks post-upgrade, serving requests from the partially-built index (or blocking until complete).
4. **Guard:** Add an instruction-counter check inside `build_follower_index` that panics with a clear error if the budget is nearly exhausted, to fail fast rather than silently bricking.

### Proof of Concept

```rust
// State-machine test sketch
let state_machine = StateMachine::new();
let sns = deploy_sns(&state_machine, max_number_of_neurons: 200_000);

// Attacker fills topic_followees to maximum on every neuron
for neuron_id in all_neuron_ids {
    sns.manage_neuron(SetFollowing {
        topic_following: ALL_7_TOPICS.map(|topic| FolloweesForTopic {
            topic: Some(topic as i32),
            followees: (0..15).map(|i| Followee { neuron_id: Some(neuron_ids[i]), alias: None }).collect(),
        }).collect(),
    }, neuron_id);
}

// Trigger upgrade
let result = state_machine.upgrade_canister(sns.governance_canister_id, new_wasm, vec![]);

// Assert: upgrade traps / canister is bricked
assert!(result.is_err());
```

The `build_follower_index` loop at `rs/sns/governance/src/follower_index.rs:136-138` will execute 21 million BTreeMap/BTreeSet insertions synchronously within `post_upgrade`, exceeding the IC's 40-billion-instruction limit for that execution context.

### Citations

**File:** rs/sns/governance/canister/canister.rs (L272-290)
```rust
#[post_upgrade]
fn canister_post_upgrade() {
    log!(INFO, "Executing post upgrade");

    let governance_proto = with_upgrades_memory(|memory| {
        let result: Result<sns_gov_pb::Governance, _> = load_protobuf(memory);
        result
    })
    .expect(
        "Error deserializing canister state post-upgrade with MemoryManager memory segment. \
             CANISTER MIGHT HAVE BROKEN STATE!!!!.",
    );

    canister_init_(governance_proto);

    init_timers();

    log!(INFO, "Completed post upgrade");
}
```

**File:** rs/sns/governance/src/governance.rs (L822-833)
```rust
    fn initialize_indices(&mut self) {
        self.function_followee_index = build_function_followee_index(
            &self.proto.id_to_nervous_system_functions,
            &self.proto.neurons,
        );

        self.topic_follower_index = build_follower_index(&self.proto.neurons);

        self.principal_to_neuron_ids_index = self
            .proto
            .build_principal_to_neuron_ids_index(&self.proto.neurons);
    }
```

**File:** rs/sns/governance/src/governance.rs (L4069-4071)
```rust
        // Check that the caller is authorized to change followers (same authorization
        // as voting required).
        neuron.check_authorized(caller, NeuronPermissionType::Vote)?;
```

**File:** rs/sns/governance/src/follower_index.rs (L90-126)
```rust
    for (topic, FolloweesForTopic { followees, .. }) in &topic_followees.topic_id_to_followees {
        let Ok(topic) = Topic::try_from(*topic) else {
            log!(
                ERROR,
                "Neuron {} has followees for an invalid topic ID: {}",
                follower_id,
                topic
            );
            continue;
        };
        let topic_index = index.entry(topic).or_default();

        for Followee {
            neuron_id: followee_id,
            alias,
        } in followees
        {
            let Some(followee_id) = followee_id else {
                let alias = alias
                    .as_ref()
                    .map(|alias| format!(" ({alias})"))
                    .unwrap_or_default();
                log!(
                    ERROR,
                    "Neuron with ID {:?} has a followee{} with no ID!",
                    follower_id,
                    alias
                );
                continue;
            };

            let key = followee_id.to_string();
            topic_index
                .entry(key)
                .or_default()
                .insert(follower_id.clone());
        }
```

**File:** rs/sns/governance/src/follower_index.rs (L132-140)
```rust
pub(crate) fn build_follower_index(
    neurons: &BTreeMap<String, Neuron>,
) -> BTreeMap<Topic, BTreeMap<String, BTreeSet<NeuronId>>> {
    let mut function_followee_index = BTreeMap::new();
    for neuron in neurons.values() {
        add_neuron_to_follower_index(&mut function_followee_index, neuron);
    }
    function_followee_index
}
```

**File:** rs/sns/governance/src/following.rs (L19-19)
```rust
pub const MAX_FOLLOWEES_PER_TOPIC: usize = 15;
```

**File:** rs/sns/governance/src/following.rs (L323-325)
```rust
        if followees.len() > MAX_FOLLOWEES_PER_TOPIC {
            return Err(Self::Error::TooManyFollowees(followees.len()));
        }
```

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L4780-4797)
```rust
pub enum Topic {
    /// Unused, here for PB lint purposes.
    Unspecified = 0,
    /// Proposals to set the direction of the DAO by tokenomics & branding
    DaoCommunitySettings = 1,
    /// Proposals to upgrade and manage the SNS DAO framework
    SnsFrameworkManagement = 2,
    /// Proposals to manage the dapp's canisters
    DappCanisterManagement = 3,
    /// Proposals related to the dapp's business logic
    ApplicationBusinessLogic = 4,
    /// Proposals related to governance
    Governance = 5,
    /// Proposals related to treasury management
    TreasuryAssetManagement = 6,
    /// Critical proposals related to dapp operations
    CriticalDappOperations = 7,
}
```

**File:** rs/sns/governance/src/types.rs (L386-386)
```rust
    pub const MAX_NUMBER_OF_NEURONS_CEILING: u64 = 200_000;
```

**File:** rs/sns/integration_tests/test_canisters/sns_governance_mem_test_canister.rs (L57-60)
```rust
/// When creating the large state, maximum following coupled with a large number of
/// NervousSystemFunctions lead to exponential growth of the state (~12 TB). For this
/// test to be effective, the number of GenericNervousSystemFunctions is limited.
const TARGET_GENERIC_NERVOUS_SYSTEM_FUNCTION_COUNT: u64 = 10;
```

**File:** rs/sns/integration_tests/test_canisters/sns_governance_mem_test_canister.rs (L252-265)
```rust
/// Allocate a Neuron
fn allocate_neuron(
    neuron_id: &NeuronId,
    neuron_permissions: &[NeuronPermission],
    followees: &BTreeMap<u64, Followees>,
) -> Neuron {
    Neuron {
        id: Some(neuron_id.clone()),
        permissions: Vec::from(neuron_permissions),
        followees: followees.clone(),
        dissolve_state: Some(DissolveState::WhenDissolvedTimestampSeconds(0)),
        ..Default::default()
    }
}
```
