### Title
Missing Anonymous Principal Check in `do_update_node_operator_config_directly` Enables Node Operator Record Hijacking — (File: rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs)

---

### Summary

The `do_update_node_operator_config_directly` function in the registry canister validates that the new `node_provider_id` is not equal to the `node_operator_id`, but performs **no check that `node_provider_id` is not the anonymous principal**. If a node operator accidentally supplies the anonymous principal as the new `node_provider_id`, the record's sole authorization guard becomes trivially bypassable by any unprivileged anonymous ingress caller, enabling full hijack of the node operator record.

---

### Finding Description

The inner function `do_update_node_operator_config_directly_` performs the following validation on the incoming `node_provider_id`: [1](#0-0) 

```rust
let node_provider_id = payload
    .node_provider_id
    .ok_or("No Node Provider specified in the payload".to_string())?;

if node_provider_id == node_operator_id {
    return Err(format!(
        "The Node Operator ID cannot be the same as the Node Provider ID: {node_operator_id}"
    ));
}

node_operator_record.node_provider_principal_id = node_provider_id.to_vec();
```

The only constraint checked is `node_provider_id != node_operator_id`. There is **no check for `node_provider_id == PrincipalId::new_anonymous()`**.

The sole authorization guard for this endpoint is: [2](#0-1) 

```rust
if caller
    != PrincipalId::try_from(&node_operator_record.node_provider_principal_id).unwrap()
{
    return Err(format!(
        "Caller {caller} not equal to the node_provider_princpal_id for this record."
    ));
}
```

If `node_provider_principal_id` is set to the anonymous principal, any caller presenting the anonymous principal (which the IC protocol permits for update calls) satisfies this check and gains full write access to the record.

The caller is obtained directly from the IC runtime with no prior anonymous-caller rejection: [3](#0-2) 

---

### Impact Explanation

**Vulnerability class:** Insufficient constraint check / missing anonymous principal guard — analogous to the missing zero-address check in the Fantium report.

**Attack chain:**

1. A node provider (legitimate user) accidentally supplies `PrincipalId::new_anonymous()` as `node_provider_id` in a call to `do_update_node_operator_config_directly`. The missing check allows this to succeed and the registry record is written with the anonymous principal as `node_provider_principal_id`.

2. An unprivileged attacker sends an anonymous ingress update call to the same endpoint, targeting the same `node_operator_id`. The authorization check `caller != anonymous` evaluates to `false` (caller IS anonymous, record IS anonymous), so the check passes.

3. The attacker sets `node_provider_id` to their own principal, taking ownership of the node operator record.

**Consequences:**
- The attacker now controls the `node_provider_principal_id` field of the node operator record in the registry.
- Node reward calculations reference this field; redirecting it can divert node provider rewards.
- The original node provider is permanently locked out of their own record (they can no longer satisfy the authorization check since the stored principal no longer matches theirs).
- The attacker can repeat the process to further manipulate the record.

---

### Likelihood Explanation

**Medium-Low.** The precondition is a human error by a node operator — exactly the class of error the external report identifies as being made more probable by missing constraint checks. Node operators interact with this endpoint directly (no governance proposal required), and the `node_provider_id` field is an `Option<PrincipalId>` supplied in a Candid payload, making accidental submission of the anonymous principal a realistic mistake. Once the precondition is met, exploitation requires only a single anonymous ingress update call with no special privileges.

---

### Recommendation

Add an explicit anonymous principal rejection for `node_provider_id` immediately after the `node_operator_id` equality check:

```rust
if node_provider_id == PrincipalId::new_anonymous() {
    return Err(
        "The Node Provider ID cannot be the anonymous principal".to_string()
    );
}
```

Additionally, consider adding a top-level anonymous caller rejection at the entry point `do_update_node_operator_config_directly` (before any record lookup) to prevent anonymous principals from ever reaching the authorization logic.

---

### Proof of Concept

1. Node operator `NO` has a registry record with `node_provider_principal_id = NP` (a real principal).
2. `NP` calls `do_update_node_operator_config_directly` with `node_provider_id = PrincipalId::new_anonymous()`.
   - Check `caller(NP) != stored(NP)` → false → passes.
   - Check `anonymous != NO` → true → passes.
   - Record written: `node_provider_principal_id = anonymous`.
3. Attacker sends anonymous ingress update to `do_update_node_operator_config_directly` with `node_operator_id = NO`, `node_provider_id = ATTACKER`.
   - Check `caller(anonymous) != stored(anonymous)` → false → passes.
   - Check `ATTACKER != NO` → true → passes.
   - Record written: `node_provider_principal_id = ATTACKER`.
4. Attacker now owns the node operator record; original node provider `NP` is locked out. [4](#0-3)

### Citations

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs (L21-31)
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
