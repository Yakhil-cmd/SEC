### Title
Rate-Limit Bypass via Self-Reassignment of `node_provider_principal_id` in `do_update_node_operator_config_directly_` — (`rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs`)

---

### Summary

`update_node_operator_config_directly` is a publicly callable registry mutation that rate-limits callers by their principal ID. Because the operation itself overwrites the `node_provider_principal_id` stored in the target `NodeOperatorRecord`, an attacker who controls two principals can exhaust the first principal's rate-limit bucket, reassign the record to the second principal, and immediately call again from the second principal's fresh bucket. This cycle can be repeated indefinitely, completely defeating the rate limit.

---

### Finding Description

The inner function `do_update_node_operator_config_directly_` executes in this order:

1. **Authorization check** (lines 59–65): verifies `caller == node_operator_record.node_provider_principal_id`.
2. **Rate-limit reservation** (lines 68–70): calls `try_reserve_capacity_for_node_provider_operation(now, caller, 1)` — the bucket key is the **caller's** principal ID.
3. **Mutation** (line 83): writes `node_operator_record.node_provider_principal_id = node_provider_id.to_vec()` — the new value supplied by the attacker.
4. **Commit** (lines 95–97): commits the reservation. [1](#0-0) 

The rate-limit state is stored per-principal in a stable-memory token-bucket (`NODE_PROVIDER_RATE_LIMITER`), keyed by `node_provider_<principal_id>`. [2](#0-1) [3](#0-2) 

After step 3 succeeds, the record's `node_provider_principal_id` is `new_np`. On the very next call, `new_np` passes the authorization check (step 1) and is issued a reservation against `new_np`'s bucket — which has never been touched and is therefore full. The old bucket for `old_np` is irrelevant from this point forward.

There is no check that `new_np` must be a previously-registered or governance-approved principal; any fresh `PrincipalId` the attacker generates satisfies the only constraint (line 77: `node_provider_id != node_operator_id`). [4](#0-3) 

---

### Impact Explanation

**Reward attribution disruption.** Both the legacy `calculate_rewards_v0` path and the newer `get_rewardable_nodes_per_provider` path read `node_operator_record.node_provider_principal_id` directly from the registry to attribute node rewards. [5](#0-4) [6](#0-5) 

By rapidly cycling `node_provider_principal_id` across principals they control, an attacker can:

- Redirect reward attribution for their nodes to any principal they control (including one registered in governance as a node provider), effectively stealing or splitting rewards.
- Point `node_provider_principal_id` at an unregistered principal, causing those nodes' rewards to be silently dropped at payout time (governance iterates only over `heap_data.node_providers`).
- Evade monitoring systems that track node-provider identity over time.

**Rate-limit nullification.** The limit is `NODE_PROVIDER_MAX_SPIKE = 140` operations (20/day average). With the bypass, an attacker can make an unbounded number of registry mutations per second, one per fresh principal. [7](#0-6) 

---

### Likelihood Explanation

- **Attacker prerequisites**: must already hold a `NodeOperatorRecord` with their principal as `node_provider_principal_id`. This is a legitimate but non-privileged role.
- **No admin access required**: the function is explicitly open to any caller (`// This method can be called by anyone`).
- **Trivially automatable**: generating fresh `PrincipalId` values is free; each cycle costs one ingress message.
- **No governance involvement needed**: the entire attack is a sequence of direct canister update calls. [8](#0-7) 

---

### Recommendation

The rate-limit reservation must be keyed on the **node operator record's key** (i.e., `node_operator_id`), not on the transient caller identity. Alternatively, the rate-limit check should be performed **after** the new `node_provider_id` is validated but **before** the mutation is applied, and the bucket key should be the stable `node_operator_id` (the record's primary key), which cannot be changed by this operation. A secondary check should also verify that the incoming `node_provider_id` is a governance-registered node provider before accepting the reassignment.

---

### Proof of Concept

```rust
// State-machine test sketch
let mut registry = invariant_compliant_registry(0);
let now = now_system_time();
let node_operator_id = PrincipalId::new_user_test_id(1);
let old_np = PrincipalId::new_user_test_id(100);
let new_np = PrincipalId::new_user_test_id(101); // fresh principal, full bucket

// Setup: NodeOperatorRecord with node_provider_principal_id = old_np
registry.do_add_node_operator(AddNodeOperatorPayload {
    node_operator_principal_id: Some(node_operator_id),
    node_provider_principal_id: Some(old_np), ..
});

// Exhaust old_np's bucket
let cap = registry.get_available_node_provider_op_capacity(old_np, now);
let res = registry.try_reserve_capacity_for_node_provider_operation(now, old_np, cap).unwrap();
registry.commit_used_capacity_for_node_provider_operation(now, res).unwrap();
// old_np bucket is now 0

// Step 1: reassign to new_np using old_np's last slot — but bucket is 0, so this
// would fail. Instead, leave 1 slot and use it for the reassignment:
// (repeat with cap-1 exhaustion, then reassign)
registry.do_update_node_operator_config_directly_(
    UpdateNodeOperatorConfigDirectlyPayload {
        node_operator_id: Some(node_operator_id),
        node_provider_id: Some(new_np),
    },
    old_np, now,
).unwrap(); // consumes old_np's last slot; record now points to new_np

// Step 2: call from new_np — fresh bucket, succeeds despite old_np being exhausted
let next_np = PrincipalId::new_user_test_id(102);
registry.do_update_node_operator_config_directly_(
    UpdateNodeOperatorConfigDirectlyPayload {
        node_operator_id: Some(node_operator_id),
        node_provider_id: Some(next_np),
    },
    new_np, now,
).unwrap(); // SUCCEEDS — rate limit bypassed
```

### Citations

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs (L58-70)
```rust
        // 2. Make sure that the caller is authorized to make the requested changes to node_operator_record.
        if caller
            != PrincipalId::try_from(&node_operator_record.node_provider_principal_id).unwrap()
        {
            return Err(format!(
                "Caller {caller} not equal to the node_provider_princpal_id for this record."
            ));
        }

        // 3. Check Rate Limits
        let current_node_provider = caller;
        let reservation =
            self.try_reserve_capacity_for_node_provider_operation(now, current_node_provider, 1)?;
```

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs (L77-83)
```rust
        if node_provider_id == node_operator_id {
            return Err(format!(
                "The Node Operator ID cannot be the same as the Node Provider ID: {node_operator_id}"
            ));
        }

        node_operator_record.node_provider_principal_id = node_provider_id.to_vec();
```

**File:** rs/registry/canister/src/rate_limits.rs (L18-21)
```rust
const NODE_PROVIDER_MAX_AVG_OPERATIONS_PER_DAY: u64 = 20;
const NODE_PROVIDER_MAX_SPIKE: u64 = NODE_PROVIDER_MAX_AVG_OPERATIONS_PER_DAY * 7;
pub const NODE_PROVIDER_CAPACITY_ADD_INTERVAL_SECONDS: u64 =
    ONE_DAY_SECONDS / NODE_PROVIDER_MAX_AVG_OPERATIONS_PER_DAY;
```

**File:** rs/registry/canister/src/rate_limits.rs (L132-141)
```rust
    pub fn try_reserve_capacity_for_node_provider_operation(
        &self,
        now: SystemTime,
        node_provider_id: PrincipalId,
        requested_capacity: u64,
    ) -> Result<Reservation<String>, RateLimiterError> {
        with_node_provider_rate_limiter(|rate_limiter| {
            rate_limiter.try_reserve(now, node_provider_key(node_provider_id), requested_capacity)
        })
    }
```

**File:** rs/registry/node_provider_rewards/src/lib.rs (L38-55)
```rust
        let node_provider_id = PrincipalId::try_from(&node_operator.node_provider_principal_id)
            .map_err(|e| {
                format!(
                    "Node Operator with key '{node_operator_id}' has a node_provider_principal_id \
                                 that cannot be parsed as a PrincipalId: '{e}'"
                )
            })?;

        let dc = data_centers.get(&node_operator.dc_id).ok_or_else(|| {
            format!(
                "Node Operator with key '{}' has data center ID '{}' \
                            not found in the Registry",
                node_operator_id, node_operator.dc_id
            )
        })?;
        let region = &dc.region;

        let np_rewards = rewards.entry(node_provider_id).or_default();
```

**File:** rs/node_rewards/canister/src/registry_querier.rs (L190-200)
```rust
            let node_provider_id: PrincipalId = node_operator_record
                .node_provider_principal_id
                .clone()
                .try_into()
                .expect("Failed to parse PrincipalId");

            if let Some(provider_filter) = provider_filter
                && &node_provider_id != provider_filter
            {
                continue;
            }
```

**File:** rs/registry/canister/canister/canister.rs (L809-823)
```rust
#[unsafe(export_name = "canister_update update_node_operator_config_directly")]
fn update_node_operator_config_directly() {
    // This method can be called by anyone
    println!(
        "{}call: update_node_operator_config_directly from: {}",
        LOG_PREFIX,
        dfn_core::api::caller()
    );
    over(
        candid_one,
        |payload: UpdateNodeOperatorConfigDirectlyPayload| {
            update_node_operator_config_directly_(payload)
        },
    );
}
```
