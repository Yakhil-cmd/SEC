The code evidence is conclusive. Here is the analysis:

---

### Title
Rate-Limit Bucket Charged to Node's Original Operator Instead of Caller in `do_remove_node_directly_` — (`rs/registry/canister/src/mutations/node_management/do_remove_node_directly.rs`)

### Summary
`do_remove_node_directly_` charges the rate-limit reservation against the **node's registered operator** (`node_operator_id`), not the **actual caller** (`caller_id`). Because `make_remove_or_replace_node_mutations` permits a different operator (operator B) to remove nodes belonging to operator A when they share the same DC and node provider, operator B can silently drain operator A's rate-limit bucket without touching its own.

### Finding Description

In `do_remove_node_directly_`, the rate-limit identity is taken from the node record, not from the ingress caller:

```rust
// line 52-54
let node_operator_id = get_node_operator_id_for_node(self, payload.node_id)?;
let reservation =
    self.try_reserve_capacity_for_node_operator_operation(now, node_operator_id, 1)?;
``` [1](#0-0) 

`caller_id` is passed only to `make_remove_or_replace_node_mutations`, which applies the DC+provider fallback authorization check:

```rust
// line 89-119: if caller_id != node_operator_id, check same DC + same provider
if caller_id != node_operator_id { ... assert_eq!(dc_caller, dc_orig_node_operator) ... }
``` [2](#0-1) 

So when operator B (same DC, same provider) removes a node owned by operator A:
- Authorization passes (DC + provider match).
- Rate-limit capacity is consumed from **operator A's bucket**, not operator B's.
- Operator B's own bucket is completely untouched.

The per-operator bucket is capped at `NODE_OPERATOR_MAX_SPIKE = 20 × 7 = 140` operations: [3](#0-2) 

`try_reserve_capacity_for_node_operator_operation` keys the reservation on `node_operator_key(node_operator_id)`, confirming the charge goes to operator A's identity: [4](#0-3) 

### Impact Explanation

Operator B can call `do_remove_node_directly` on up to 140 of operator A's nodes (or however many operator A owns). Each call:
1. Passes authorization (same DC + same provider).
2. Deducts 1 from operator A's 140-op weekly bucket.
3. Deducts 1 from the shared node-provider bucket (hurting operator A further).
4. Leaves operator B's own operator bucket at full capacity.

Once operator A's bucket reaches 0, `try_reserve_capacity_for_node_operator_operation` returns `NotEnoughCapacity` for any call keyed to operator A — including operator A's own `do_add_node` and `do_remove_node_directly` calls — for up to 7 days (until the token-bucket refills at 20 ops/day).

The shared node-provider bucket is also depleted, which can cascade to other operators under the same provider.

### Likelihood Explanation

The precondition — two node operators sharing the same DC and node provider — is an explicitly supported and documented redeployment scenario (see the comment at line 83–88 of `do_remove_node_directly.rs`). [5](#0-4) 

No governance vote, admin key, or threshold corruption is required. Operator B only needs to be a registered node operator in the same DC under the same provider, which is a normal on-chain state. The attack is a sequence of ordinary ingress update calls to the registry canister.

### Recommendation

Charge the rate limit against `caller_id` (the actual caller), not `node_operator_id` (the node's registered operator). In `do_remove_node_directly_`, replace:

```rust
let node_operator_id = get_node_operator_id_for_node(self, payload.node_id)?;
let reservation =
    self.try_reserve_capacity_for_node_operator_operation(now, node_operator_id, 1)?;
```

with:

```rust
let reservation =
    self.try_reserve_capacity_for_node_operator_operation(now, caller_id, 1)?;
```

This ensures the cost of the operation is always borne by the entity initiating it.

### Proof of Concept

```rust
// Two operators, same DC "DC1", same provider
let operator_a = PrincipalId::new_user_test_id(1);
let operator_b = PrincipalId::new_user_test_id(2);
let provider   = PrincipalId::new_user_test_id(100);

// Register both operators in DC1 under the same provider
// Add 140 nodes owned by operator_a
// operator_b calls do_remove_node_directly_ on each of operator_a's nodes

for node_id in operator_a_nodes {
    registry.do_remove_node_directly_(
        RemoveNodeDirectlyPayload { node_id },
        operator_b,   // caller
        now,
    ).unwrap();
}

// operator_a's bucket is now 0; operator_b's bucket is untouched
let result = registry.do_remove_node_directly_(
    RemoveNodeDirectlyPayload { node_id: some_new_node_of_a },
    operator_a,
    now,
);
assert_eq!(result.unwrap_err(), "Rate Limit Capacity exceeded. Please wait and try again later.");

let b_capacity = registry.get_available_node_operator_op_capacity(operator_b, now);
assert_eq!(b_capacity, 140); // operator_b untouched
```

### Citations

**File:** rs/registry/canister/src/mutations/node_management/do_remove_node_directly.rs (L52-54)
```rust
        let node_operator_id = get_node_operator_id_for_node(self, payload.node_id)?;
        let reservation =
            self.try_reserve_capacity_for_node_operator_operation(now, node_operator_id, 1)?;
```

**File:** rs/registry/canister/src/mutations/node_management/do_remove_node_directly.rs (L83-88)
```rust
        // 2. Compare the caller_id (node operator) with the node's node operator and, if that fails,
        // fall back to comparing the DC and the node provider ID for the caller and the node.
        // That covers the case when the node provider added a new operator record in the same DC, and
        // is trying to redeploy the nodes under the new operator.
        // Hence, if the DC and the node provider of the caller and the original node operator match,
        // the removal should succeed.
```

**File:** rs/registry/canister/src/mutations/node_management/do_remove_node_directly.rs (L89-119)
```rust
        if caller_id != node_operator_id {
            let node_operator_caller = get_node_operator_record(self, caller_id)
                .map_err(|e| {
                    format!("{LOG_PREFIX}do_remove_node_directly: Aborting node removal: {e}")
                })
                .unwrap();
            let dc_caller = node_operator_caller.dc_id;
            let dc_orig_node_operator = get_node_operator_record(self, node_operator_id)
                .map_err(|e| {
                    format!("{LOG_PREFIX}do_remove_node_directly: Aborting node removal: {e}")
                })
                .unwrap()
                .dc_id;
            assert_eq!(
                dc_caller, dc_orig_node_operator,
                "The DC {dc_caller} of the caller {caller_id}, does not match the DC of the node {dc_orig_node_operator}."
            );
            let node_provider_caller = get_node_provider_id_for_operator_id(self, caller_id)
                .map_err(|e| {
                    format!("{LOG_PREFIX}do_remove_node_directly: Aborting node removal: {e}")
                });
            let node_provider_of_the_node =
                get_node_provider_id_for_operator_id(self, node_operator_id).map_err(|e| {
                    format!("{LOG_PREFIX}do_remove_node_directly: Aborting node removal: {e}")
                });
            assert_eq!(
                node_provider_caller, node_provider_of_the_node,
                "The node provider {:?} of the caller {}, does not match the node provider {:?} of the node {}.",
                node_provider_caller, caller_id, node_provider_of_the_node, payload.node_id
            );
        }
```

**File:** rs/registry/canister/src/rate_limits.rs (L24-25)
```rust
const NODE_OPERATOR_MAX_AVG_OPERATIONS_PER_DAY: u64 = 20;
const NODE_OPERATOR_MAX_SPIKE: u64 = NODE_OPERATOR_MAX_AVG_OPERATIONS_PER_DAY * 7;
```

**File:** rs/registry/canister/src/rate_limits.rs (L114-117)
```rust
        // First reserve from node operator rate limiter (primary)
        let operator_reservation = with_node_operator_rate_limiter(|rate_limiter| {
            rate_limiter.try_reserve(now, node_operator_key(node_operator_id), requested_capacity)
        })?;
```
