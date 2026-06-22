### Title
Missing Anonymous Principal Validation in `update_node_operator_config_directly` Allows Node Operator Record Takeover - (File: `rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs`)

### Summary
The `update_node_operator_config_directly` endpoint in the Registry canister is callable by any ingress sender and allows the current node provider to update the `node_provider_principal_id` field of a node operator record. The function validates that the new `node_provider_id` is not equal to the `node_operator_id`, but contains no check preventing it from being set to the anonymous principal. If a node provider sets their `node_provider_principal_id` to the anonymous principal — accidentally or intentionally — any subsequent anonymous ingress call passes the authorization check, enabling a full node operator record takeover.

### Finding Description
The canister endpoint `update_node_operator_config_directly` is explicitly documented as callable by anyone:

```
// This method can be called by anyone
``` [1](#0-0) 

The inner implementation `do_update_node_operator_config_directly_` performs an authorization check by comparing the ingress caller to the `node_provider_principal_id` stored in the registry record:

```rust
if caller
    != PrincipalId::try_from(&node_operator_record.node_provider_principal_id).unwrap()
{
    return Err(...)
}
``` [2](#0-1) 

After passing this check, the function validates only that the new `node_provider_id` is not equal to the `node_operator_id`, but performs **no check that `node_provider_id` is not the anonymous principal**:

```rust
if node_provider_id == node_operator_id {
    return Err(...)
}
node_operator_record.node_provider_principal_id = node_provider_id.to_vec();
``` [3](#0-2) 

The `UpdateNodeOperatorConfigDirectlyPayload` struct accepts any `Option<PrincipalId>` for `node_provider_id`, including `PrincipalId::anonymous()`: [4](#0-3) 

The IC ingress validation explicitly permits anonymous callers to submit update calls without a signature:

```rust
None => {
    if sender.get().is_anonymous() {
        return Ok(CanisterIdSet::all());
    }
    Err(MissingSignature(*sender))
}
``` [5](#0-4) 

This means `dfn_core::api::caller()` returns `PrincipalId::anonymous()` for unsigned ingress messages, and the authorization check at line 59–65 passes when `node_provider_principal_id` in the stored record is also the anonymous principal.

### Impact Explanation
If a node provider sets their `node_provider_principal_id` to the anonymous principal (e.g., when attempting to transfer ownership and supplying the wrong value), the node operator record becomes controllable by any anonymous ingress sender. An attacker can then call `update_node_operator_config_directly` anonymously, pass the authorization check, and overwrite `node_provider_principal_id` with a principal they control. This gives the attacker full ownership of the node operator record, including the ability to further reassign it, and corrupts the registry's authoritative mapping of node operators to node providers — a critical piece of IC infrastructure used for node allowance accounting and reward distribution.

### Likelihood Explanation
The likelihood is **low-medium**. The attack requires the current node provider to first set their `node_provider_principal_id` to the anonymous principal. This is a realistic mistake when a node provider attempts to transfer ownership of a node operator record to another party and supplies an incorrect or zero-equivalent principal. The `update_node_operator_config_directly` endpoint is open to all callers, and no governance proposal is required, so exploitation after the precondition is met is trivial and immediate.

### Recommendation
Add an explicit check that the new `node_provider_id` is not the anonymous principal, analogous to the zero-address check recommended in the original report:

```rust
if node_provider_id == PrincipalId::anonymous() {
    return Err("Node Provider ID cannot be the anonymous principal".to_string());
}
if node_provider_id == node_operator_id {
    return Err(...)
}
``` [3](#0-2) 

### Proof of Concept

**Step 1 — Precondition (node provider sets `node_provider_principal_id` to anonymous):**
```
Node Provider A calls update_node_operator_config_directly with:
  node_operator_id = <existing_operator_principal>
  node_provider_id = PrincipalId::anonymous()   // [0x04]
```
The function accepts this because `anonymous != node_operator_id` and no other check exists.

**Step 2 — Attacker exploits the open record:**
```
Attacker sends an unsigned (anonymous) ingress message:
  update_node_operator_config_directly({
    node_operator_id: <existing_operator_principal>,
    node_provider_id: <attacker_principal>
  })
```

**Step 3 — Authorization check passes:**
- `dfn_core::api::caller()` → `PrincipalId::anonymous()`
- `PrincipalId::try_from(&node_operator_record.node_provider_principal_id)` → `PrincipalId::anonymous()`
- `caller != stored_provider` → `false` → check passes

**Step 4 — Attacker owns the record:**
`node_provider_principal_id` is now set to `<attacker_principal>`. The attacker has full control over the node operator record and can make further changes or reassign it at will. [6](#0-5)

### Citations

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

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs (L21-100)
```rust
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

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs (L106-116)
```rust
#[derive(Clone, Eq, PartialEq, CandidType, Deserialize, Message, Serialize)]
pub struct UpdateNodeOperatorConfigDirectlyPayload {
    /// The principal id of the node operator. This principal is the entity that
    /// is able to add and remove nodes.
    #[prost(message, optional, tag = "1")]
    pub node_operator_id: Option<PrincipalId>,

    /// The principal id of this node's provider.
    #[prost(message, optional, tag = "2")]
    pub node_provider_id: Option<PrincipalId>,
}
```

**File:** rs/validator/src/ingress_validation.rs (L853-858)
```rust
    match signature {
        None => {
            if sender.get().is_anonymous() {
                return Ok(CanisterIdSet::all());
            }
            Err(MissingSignature(*sender))
```
