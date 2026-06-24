### Title
Malicious Node Provider Can Frontrun `remove_node_operators` Governance Proposal via Self-Service `migrate_node_operator_directly` - (File: rs/registry/canister/src/mutations/do_migrate_node_operator_directly.rs)

---

### Summary

The registry canister exposes `migrate_node_operator_directly` as a publicly callable update method with no governance-level caller restriction. Any node provider can call it to delete their own node operator record and recreate it under a new principal. Because `do_remove_node_operators` silently skips node operator IDs that no longer exist in the registry, a malicious node provider can observe a pending NNS governance `remove_node_operators` proposal and preemptively migrate their node operator to a fresh principal, causing the governance action to have no effect while they continue operating.

---

### Finding Description

The canister entry point for `migrate_node_operator_directly` carries no governance caller check:

```rust
// canister.rs line 844-854
#[unsafe(export_name = "canister_update migrate_node_operator_directly")]
fn migrate_node_operator_directly() {
    over(candid_one, |payload: MigrateNodeOperatorPayload| {
        migrate_node_operator_directly_(payload)
    });
}
```

The comment on the analogous `update_node_operator_config_directly` entry point even states explicitly: `// This method can be called by anyone`. The only authorization check inside `migrate_node_operator_inner` is that the caller equals the `node_provider_principal_id` stored in the old node operator record — i.e., the very party that governance may be trying to remove is the one authorized to call this function.

When the migration succeeds, the old node operator record is **deleted** from the registry and a new record is created under the new principal:

```rust
// do_migrate_node_operator_directly.rs
total_mutations.extend(self.get_node_mutations(old_node_operator_id, new_node_operator_id));
self.maybe_apply_mutation_internal(total_mutations);
```

The governance-only `remove_node_operators` path then silently skips any principal whose record no longer exists:

```rust
// do_remove_node_operators.rs lines 25-34
let mut valid_node_operator_ids_to_remove: Vec<PrincipalId> = payload
    .principal_ids_to_remove()
    .into_iter()
    .filter(|node_operator_id| {
        let node_operator_record_key =
            make_node_operator_record_key(*node_operator_id).into_bytes();
        self.get(&node_operator_record_key, self.latest_version())
            .is_some()
    })
    .collect();
```

The proposal execution returns success with zero mutations applied for the targeted principal.

---

### Impact Explanation

A malicious node provider can indefinitely evade registry removal. Each time governance submits a `remove_node_operators` proposal targeting their current node operator principal, the node provider migrates to a fresh principal before the proposal executes. The governance proposal succeeds silently with no on-chain error, the NNS community receives no indication of failure, and the malicious operator continues to:

- Hold active node operator records in the registry
- Earn node provider rewards (if still listed as a node provider in governance)
- Operate nodes on the IC network

This is a governance authorization bypass with direct impact on the integrity of the node operator registry and the NNS's ability to enforce network participant management.

---

### Likelihood Explanation

NNS governance proposals are fully public and have a voting period measured in days (the standard wait-for-quiet threshold). A malicious node provider has ample time to observe a `remove_node_operators` proposal on the NNS dashboard and submit `migrate_node_operator_directly` before the proposal reaches execution. The only guard that could slow the attack — the 12-hour minimum age requirement on the old operator record — is satisfied by every real node provider already operating on the network. The rate limit is per-provider and only needs to be consumed once per evasion. No special tooling or privileged access is required beyond the node provider's own principal key.

---

### Recommendation

Restrict `migrate_node_operator_directly` to governance-only callers, mirroring the pattern used for `remove_node_operators` and `update_node_operator_config`:

```rust
// canister.rs
#[unsafe(export_name = "canister_update migrate_node_operator_directly")]
fn migrate_node_operator_directly() {
+   check_caller_is_governance_and_log("migrate_node_operator_directly");
    over(candid_one, |payload: MigrateNodeOperatorPayload| {
        migrate_node_operator_directly_(payload)
    });
}
```

If self-service migration must be preserved for legitimate operational use, a complementary mitigation is to add a registry-level lock or precondition check that prevents migration of a node operator whose principal ID appears in any pending governance proposal payload.

---

### Proof of Concept

1. Node provider `NP` owns node operator record `NO` in the registry.
2. NNS governance submits a `remove_node_operators` proposal listing `NO` as a principal to remove. The proposal enters the voting period (days).
3. `NP` calls `migrate_node_operator_directly` with `old_node_operator_id = NO`, `new_node_operator_id = NO2` (a fresh principal controlled by `NP`). The call succeeds because `NP == node_operator_record[NO].node_provider_principal_id`.
4. `do_migrate_node_operator_inner` deletes the registry record for `NO` and creates a new record for `NO2`, transferring all nodes and allowances.
5. The governance proposal reaches execution. `do_remove_node_operators` filters its input list: `NO` is no longer present in the registry, so it is dropped from `valid_node_operator_ids_to_remove`. Zero mutations are applied. The proposal status is `Executed` with no visible error.
6. `NP` continues operating under `NO2`, unaffected by the governance action. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** rs/registry/canister/canister/canister.rs (L844-855)
```rust
#[unsafe(export_name = "canister_update migrate_node_operator_directly")]
fn migrate_node_operator_directly() {
    over(candid_one, |payload: MigrateNodeOperatorPayload| {
        migrate_node_operator_directly_(payload)
    });
}

#[candid_method(update, rename = "migrate_node_operator_directly")]
fn migrate_node_operator_directly_(payload: MigrateNodeOperatorPayload) {
    registry_mut().do_migrate_node_operator_directly(payload);
    recertify_registry();
}
```

**File:** rs/registry/canister/canister/canister.rs (L857-863)
```rust
#[unsafe(export_name = "canister_update remove_node_operators")]
fn remove_node_operators() {
    check_caller_is_governance_and_log("remove_node_operators");
    over(candid_one, |payload: RemoveNodeOperatorsPayload| {
        remove_node_operators_(payload)
    });
}
```

**File:** rs/registry/canister/src/mutations/do_migrate_node_operator_directly.rs (L66-97)
```rust
    pub fn do_migrate_node_operator_directly(&mut self, payload: MigrateNodeOperatorPayload) {
        self.migrate_node_operator_inner(payload, dfn_core::api::caller(), now_system_time())
            .unwrap_or_else(|e| panic!("{e}"));
    }

    /// Internal implementation of node operator migration with injectable dependencies.
    fn migrate_node_operator_inner(
        &mut self,
        payload: MigrateNodeOperatorPayload,
        caller: PrincipalId,
        now: SystemTime,
    ) -> Result<(), MigrateError> {
        // Check if the payload is valid by itself.
        payload.validate()?;
        let (old_node_operator_id, new_node_operator_id) = (
            payload.old_node_operator_id.unwrap(),
            payload.new_node_operator_id.unwrap(),
        );

        let mut total_mutations = self.get_operator_mutations_if_business_rules_are_valid(
            old_node_operator_id,
            new_node_operator_id,
            caller,
            now,
        )?;

        total_mutations.extend(self.get_node_mutations(old_node_operator_id, new_node_operator_id));

        self.maybe_apply_mutation_internal(total_mutations);

        Ok(())
    }
```

**File:** rs/registry/canister/src/mutations/do_migrate_node_operator_directly.rs (L132-141)
```rust
        // The caller must be the owner of both of the node operator
        // records.
        if caller.to_vec() != old_node_operator_record.node_provider_principal_id {
            return Err(MigrateError::NotAuthorized {
                caller,
                expected: PrincipalId(Principal::from_slice(
                    &old_node_operator_record.node_provider_principal_id,
                )),
            });
        }
```

**File:** rs/registry/canister/src/mutations/do_remove_node_operators.rs (L24-48)
```rust
        // Filter Node Operator IDs that have a NodeOperatorRecord in the Registry
        let mut valid_node_operator_ids_to_remove: Vec<PrincipalId> = payload
            .principal_ids_to_remove()
            .into_iter()
            .filter(|node_operator_id| {
                let node_operator_record_key =
                    make_node_operator_record_key(*node_operator_id).into_bytes();
                self.get(&node_operator_record_key, self.latest_version())
                    .is_some()
            })
            .collect();

        self.filter_out_node_operators_that_have_nodes(&mut valid_node_operator_ids_to_remove);

        for node_operator_id in valid_node_operator_ids_to_remove {
            let node_operator_record_key =
                make_node_operator_record_key(node_operator_id).into_bytes();
            mutations.push(RegistryMutation {
                mutation_type: registry_mutation::Type::Delete as i32,
                key: node_operator_record_key,
                value: vec![],
            });
        }

        self.maybe_apply_mutation_internal(mutations);
```
