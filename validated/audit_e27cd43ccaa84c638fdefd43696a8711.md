### Title
One-Step Node Provider Transfer Allows Irrecoverable Loss of Node Operator Control - (File: `rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs`)

---

### Summary

The `update_node_operator_config_directly` endpoint in the IC Registry canister allows a Node Provider (NP) to atomically reassign the `node_provider_principal_id` of a Node Operator record to any new principal in a single transaction, with no confirmation step from the new principal. If an incorrect principal is specified, the original NP immediately loses all authority over that Node Operator record, and recovery requires an NNS governance proposal — a slow, high-friction process that may not be available if the NP has no NNS neuron.

---

### Finding Description

`do_update_node_operator_config_directly` is a publicly callable endpoint (no governance gating) that allows the current `node_provider_principal_id` of a `NodeOperatorRecord` to change itself to any arbitrary new principal in one step:

```rust
// rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs
// Step 2: caller must equal current node_provider_principal_id
if caller != PrincipalId::try_from(&node_operator_record.node_provider_principal_id).unwrap() {
    return Err(...);
}
// ...
// Step 4+5: immediately overwrite with new value, no acceptance required
node_operator_record.node_provider_principal_id = node_provider_id.to_vec();
self.maybe_apply_mutation_internal(mutations);
```

The `node_provider_principal_id` field is the **sole authorization gate** for this endpoint. Once overwritten, only the new principal can call `update_node_operator_config_directly` again. If the new principal is wrong (typo, burned address, canister that was deleted, etc.), the original NP has no recourse through this endpoint.

The `node_operator_principal_id` (the entity that can add/remove nodes) is controlled by the `node_provider_principal_id` through this path. Loss of the NP principal means loss of the ability to self-service manage the Node Operator record. [1](#0-0) 

The canister endpoint is exposed with no access control beyond the caller check:

```rust
// rs/registry/canister/canister/canister.rs
#[unsafe(export_name = "canister_update update_node_operator_config_directly")]
fn update_node_operator_config_directly() {
    // This method can be called by anyone
``` [2](#0-1) 

The `NodeOperatorRecord` proto confirms `node_provider_principal_id` is the privileged field: [3](#0-2) 

---

### Impact Explanation

- The `node_operator_principal_id` is the entity that can add and remove nodes from the IC network.
- The `node_provider_principal_id` is the entity that controls the Node Operator record via `update_node_operator_config_directly`.
- If a NP mistakenly sets `node_provider_id` to a wrong/inaccessible principal, they permanently lose self-service control of the Node Operator record.
- Recovery requires submitting an NNS governance proposal via `update_node_operator_config` (governance-gated path), which requires holding an NNS neuron with sufficient voting power and waiting for proposal execution. This is a high-friction, slow recovery path that may not be available to all NPs.
- In the worst case (e.g., the new principal is a burned address or a deleted canister), the Node Operator record becomes permanently unmanageable by the NP without NNS intervention. [4](#0-3) 

---

### Likelihood Explanation

- The endpoint is callable by any principal who is the current `node_provider_principal_id` — no governance vote required.
- Node Providers are real-world entities who may make typos or rotate keys incorrectly.
- The rate-limit mechanism (capacity reservation) does not prevent a single erroneous call from causing permanent lock-out.
- The analogous Hermez/Vader finding was confirmed by the team as a real risk worth addressing. [5](#0-4) 

---

### Recommendation

Implement a two-step transfer for `node_provider_principal_id`:

1. **Step 1 (propose):** The current NP calls a new endpoint `propose_node_provider_transfer(node_operator_id, new_np_id)`, which stores a `pending_node_provider_id` in the registry record but does **not** change the active `node_provider_principal_id`.
2. **Step 2 (accept):** The new NP calls `accept_node_provider_transfer(node_operator_id)`, which moves `pending_node_provider_id` into `node_provider_principal_id`.

A mistake in Step 1 can be corrected by the current NP calling Step 1 again with the correct principal before acceptance. Optionally, add a time-bounded expiry on the pending transfer.

---

### Proof of Concept

1. Node Provider A controls `NodeOperatorRecord` for `node_operator_id = X`, with `node_provider_principal_id = A`.
2. NP A calls `update_node_operator_config_directly` with `node_operator_id = X`, `node_provider_id = WRONG_PRINCIPAL` (e.g., a typo or a burned address).
3. The registry immediately writes `node_provider_principal_id = WRONG_PRINCIPAL`.
4. NP A attempts to call `update_node_operator_config_directly` again to correct the mistake — the call is rejected because `caller (A) != node_provider_principal_id (WRONG_PRINCIPAL)`.
5. NP A has no self-service recovery path. The only recourse is an NNS governance proposal via `update_node_operator_config`, which requires NNS neuron ownership and a governance vote. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs (L18-31)
```rust
impl Registry {
    /// Update an existing Node Operator's config without going through the proposal process.
    /// Only the current NP specified in a record can make changes to that record's NP field.
    pub fn do_update_node_operator_config_directly(
        &mut self,
        payload: UpdateNodeOperatorConfigDirectlyPayload,
    ) {
        self.do_update_node_operator_config_directly_(
            payload,
            dfn_core::api::caller(),
            now_system_time(),
        )
        .unwrap()
    }
```

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs (L33-100)
```rust
    fn do_update_node_operator_config_directly_(
        &mut self,
        payload: UpdateNodeOperatorConfigDirectlyPayload,
        caller: PrincipalId,
        now: SystemTime,
    ) -> Result<(), String> {
        println!("{LOG_PREFIX}do_update_node_operator_config_directly: {payload:?}");

        // 1. Look up the record of the requested target NodeOperatorRecord.
        let node_operator_id = payload
            .node_operator_id
            .ok_or("No Node Operator specified in the payload".to_string())?;

        let node_operator_record_key = make_node_operator_record_key(node_operator_id).into_bytes();
        let node_operator_record_vec = &self
            .get(&node_operator_record_key, self.latest_version())
            .ok_or(format!(
                "Node Operator record with ID {node_operator_id} not found in the registry."
            ))?
            .value;

        let mut node_operator_record =
            NodeOperatorRecord::decode(node_operator_record_vec.as_slice())
                .map_err(|e| format!("{e:?}"))?;

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

        // 4. Check that the Node Provider is not being set with the same ID as the Node Operator
        let node_provider_id = payload
            .node_provider_id
            .ok_or("No Node Provider specified in the payload".to_string())?;

        if node_provider_id == node_operator_id {
            return Err(format!(
                "The Node Operator ID cannot be the same as the Node Provider ID: {node_operator_id}"
            ));
        }

        node_operator_record.node_provider_principal_id = node_provider_id.to_vec();

        // 5. Set and execute the mutation
        let mutations = vec![RegistryMutation {
            mutation_type: registry_mutation::Type::Update as i32,
            key: node_operator_record_key,
            value: node_operator_record.encode_to_vec(),
        }];

        // Check invariants before applying mutations
        self.maybe_apply_mutation_internal(mutations);

        if let Err(e) = self.commit_used_capacity_for_node_provider_operation(now, reservation) {
            println!("{LOG_PREFIX}Error committing Rate Limit usage: {e}");
        }

        Ok(())
    }
```

**File:** rs/registry/canister/canister/canister.rs (L809-828)
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
```

**File:** rs/protobuf/def/registry/node_operator/v1/node_operator.proto (L9-22)
```text
message NodeOperatorRecord {
  // The principal id of the node operator. This principal is the entity that
  // is able to add and remove nodes.
  //
  // This must be unique across NodeOperatorRecords.
  bytes node_operator_principal_id = 1;

  // The remaining number of nodes that could be added by this node operator.
  // This number should never go below 0.
  uint64 node_allowance = 2;

  // The principal id of this node operator's provider.
  bytes node_provider_principal_id = 3;

```

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config.rs (L16-57)
```rust
impl Registry {
    /// Update an existing Node Operator's config
    pub fn do_update_node_operator_config(&mut self, payload: UpdateNodeOperatorConfigPayload) {
        println!("{LOG_PREFIX}do_update_node_operator_config: {payload:?}");

        let node_operator_id = payload.node_operator_id.unwrap();
        let node_operator_record_key = make_node_operator_record_key(node_operator_id).into_bytes();
        let RegistryValue {
            value: node_operator_record_vec,
            version: _,
            deletion_marker: _,
            timestamp_nanoseconds: _,
        } = self
            .get(&node_operator_record_key, self.latest_version())
            .unwrap_or_else(|| {
                panic!(
                    "{LOG_PREFIX}Node Operator record with ID {node_operator_id} not found in the registry."
                )
            });

        let mut node_operator_record =
            NodeOperatorRecord::decode(node_operator_record_vec.as_slice()).unwrap();

        if let Some(new_allowance) = payload.node_allowance {
            node_operator_record.node_allowance = new_allowance;
        };

        if let Some(new_dc_id) = payload.dc_id {
            node_operator_record.dc_id = new_dc_id;
        }

        if !payload.rewardable_nodes.is_empty() {
            node_operator_record.rewardable_nodes = payload.rewardable_nodes;
        }

        if let Some(node_provider_id) = payload.node_provider_id {
            assert_ne!(
                node_provider_id, node_operator_id,
                "The Node Operator ID cannot be the same as the Node Provider ID: {node_operator_id}"
            );
            node_operator_record.node_provider_principal_id = node_provider_id.to_vec();
        }
```
