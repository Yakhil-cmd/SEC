### Title
`do_update_node_operator_config_directly` Bypasses Node Provider Registration Requirement Enforced During Governance-Based `add_node_operator` - (File: rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs)

---

### Summary

The Registry canister exposes `update_node_operator_config_directly`, callable by any ingress sender who is the current `node_provider_principal_id` of a `NodeOperatorRecord`. This function allows the caller to replace `node_provider_principal_id` with any arbitrary principal without validating that the new principal is a registered node provider. This bypasses the strict governance-layer requirement enforced during `add_node_operator`, where NNS governance validates that the target `node_provider_principal_id` is a registered node provider in `gov.heap_data.node_providers`. The asymmetry mirrors the original report: registration enforces a strict identity check (signed URL / registered NP), but the direct update path silently skips it.

---

### Finding Description

**Registration path (strict):** When a node operator is added via NNS governance proposal (`add_node_operator`), the governance canister validates that `node_provider_principal_id` is a registered node provider before the mutation reaches the registry. This is confirmed by the test `test_node_provider_must_be_registered` in `rs/nns/governance/tests/governance.rs`.

**Direct update path (no validation):** `update_node_operator_config_directly` is open to any caller and performs only two checks:

1. `caller == node_operator_record.node_provider_principal_id` (identity of current NP)
2. `node_provider_id != node_operator_id` (no self-assignment) [1](#0-0) 

After passing these checks, the new `node_provider_id` from the payload is written directly into the record with no check against the NNS governance node provider registry: [2](#0-1) 

The canister entry point explicitly comments "This method can be called by anyone": [3](#0-2) 

The `NodeOperatorRecord` proto stores `node_provider_principal_id` as raw bytes with no invariant enforcement at the registry layer: [4](#0-3) 

---

### Impact Explanation

A registered node provider can call `update_node_operator_config_directly` and set `node_provider_principal_id` to any arbitrary principal — including one that has never been registered as a node provider through NNS governance. Consequences:

1. **Reward distribution corruption:** `calculate_rewards_v0` in `rs/registry/node_provider_rewards/src/lib.rs` aggregates rewards keyed by `node_provider_principal_id`. Rewards computed for an unregistered principal cannot be distributed through the normal NNS reward mechanism, causing reward loss or misattribution. [5](#0-4) 

2. **Governance invariant violation:** The NNS governance invariant that every `node_provider_principal_id` in the registry corresponds to a registered node provider is silently broken. Downstream governance operations (e.g., node provider reward proposals) that assume this invariant may behave incorrectly.

3. **Unregistered entity gains node operator control:** The new principal, though unregistered, becomes the authoritative controller of the `NodeOperatorRecord` and can make further direct updates, including transferring control again, without ever going through governance.

---

### Likelihood Explanation

Any existing node provider (an unprivileged ingress sender who holds the key for a `node_provider_principal_id` stored in any `NodeOperatorRecord`) can trigger this. No privileged access, no governance majority, and no threshold attack is required. The rate limit (`try_reserve_capacity_for_node_provider_operation`) slows but does not prevent the bypass. [6](#0-5) 

---

### Recommendation

1. **Validate new NP registration:** Before writing `node_provider_id` into the record, verify that the new principal exists in the NNS governance node provider list (analogous to the check performed during `add_node_operator` proposal validation).

2. **Emit a distinct registry mutation type or log entry** for direct NP transfers vs. governance-based additions, to preserve audit trail integrity.

3. **Consider requiring the new NP to co-sign or pre-authorize** the transfer (analogous to the original report's recommendation that `updateNode` should require the signer's permission), preventing unilateral transfers to principals that have not consented.

---

### Proof of Concept

1. Node provider `NP_A` is registered in NNS governance and owns `NodeOperatorRecord` for `NO_1` with `node_provider_principal_id = NP_A`.
2. `NP_A` calls `update_node_operator_config_directly` with `node_operator_id = NO_1`, `node_provider_id = UNREGISTERED_PRINCIPAL`.
3. The registry writes `node_provider_principal_id = UNREGISTERED_PRINCIPAL` into the record. [7](#0-6) 
4. `UNREGISTERED_PRINCIPAL` is now the authoritative NP for `NO_1` and can make further direct updates.
5. Reward calculation for `NO_1`'s rewardable nodes is now attributed to `UNREGISTERED_PRINCIPAL`, which has no registered reward account in NNS governance, causing reward loss. [5](#0-4)

### Citations

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs (L58-65)
```rust
        // 2. Make sure that the caller is authorized to make the requested changes to node_operator_record.
        if caller
            != PrincipalId::try_from(&node_operator_record.node_provider_principal_id).unwrap()
        {
            return Err(format!(
                "Caller {caller} not equal to the node_provider_princpal_id for this record."
            ));
        }
```

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs (L67-70)
```rust
        // 3. Check Rate Limits
        let current_node_provider = caller;
        let reservation =
            self.try_reserve_capacity_for_node_provider_operation(now, current_node_provider, 1)?;
```

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs (L83-93)
```rust
        node_operator_record.node_provider_principal_id = node_provider_id.to_vec();

        // 5. Set and execute the mutation
        let mutations = vec![RegistryMutation {
            mutation_type: registry_mutation::Type::Update as i32,
            key: node_operator_record_key,
            value: node_operator_record.encode_to_vec(),
        }];

        // Check invariants before applying mutations
        self.maybe_apply_mutation_internal(mutations);
```

**File:** rs/registry/canister/canister/canister.rs (L809-829)
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

#[candid_method(update, rename = "update_node_operator_config_directly")]
fn update_node_operator_config_directly_(payload: UpdateNodeOperatorConfigDirectlyPayload) {
    registry_mut().do_update_node_operator_config_directly(payload);
    recertify_registry();
}
```

**File:** rs/protobuf/def/registry/node_operator/v1/node_operator.proto (L20-22)
```text
  // The principal id of this node operator's provider.
  bytes node_provider_principal_id = 3;

```

**File:** rs/registry/node_provider_rewards/src/lib.rs (L38-44)
```rust
        let node_provider_id = PrincipalId::try_from(&node_operator.node_provider_principal_id)
            .map_err(|e| {
                format!(
                    "Node Operator with key '{node_operator_id}' has a node_provider_principal_id \
                                 that cannot be parsed as a PrincipalId: '{e}'"
                )
            })?;
```
