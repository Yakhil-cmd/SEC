### Title
Engine Controller Can Un-halt a CloudEngine Subnet Halted by NNS Governance via `set_subnet_operational_level` - (File: rs/registry/canister/src/mutations/do_update_subnet.rs)

### Summary
The Engine Controller canister (a lower-privileged role restricted to CloudEngine subnet management) can override NNS Governance's decision to halt a CloudEngine subnet by calling `update_subnet` with `is_halted = Some(false)`, even after NNS Governance has explicitly halted the subnet via `set_subnet_operational_level` for recovery or security purposes. This is a direct analog to the keep-core `registryKeeper` overriding `panicButton` pattern: a lower-privileged trusted role can undo a security action taken by a higher-privileged role.

### Finding Description
The IC registry exposes two separate mechanisms that both write the `is_halted` field of a `SubnetRecord`:

**Mechanism 1 — `set_subnet_operational_level` (Governance-only):**
The endpoint `set_subnet_operational_level` is gated by `check_caller_is_governance_and_log`, meaning only NNS Governance can call it. It is explicitly designed for subnet recovery: setting `operational_level = DOWN_FOR_REPAIRS` writes `is_halted = true` to the subnet record. [1](#0-0) [2](#0-1) [3](#0-2) 

**Mechanism 2 — `update_subnet` (Governance OR Engine Controller):**
The endpoint `update_subnet` is gated by `check_caller_is_governance_or_engine_controller_and_log`, allowing both NNS Governance and the Engine Controller canister to call it. [4](#0-3) [5](#0-4) 

When the Engine Controller calls `update_subnet`, `do_update_subnet` verifies the subnet is of type `CloudEngine` and that only `subnet_admins` and `is_halted` are set. Crucially, `is_halted` is explicitly in the **allowed** set for the Engine Controller: [6](#0-5) [7](#0-6) 

There is **no check** in `do_update_subnet` that prevents the Engine Controller from setting `is_halted = false` on a subnet that was halted by NNS Governance via `set_subnet_operational_level`. The Engine Controller proxy also explicitly allows `is_halted`: [8](#0-7) [9](#0-8) 

The Engine Controller's authorized caller is a single hardcoded principal (`bct5z-...`), configurable at init/upgrade: [10](#0-9) 

### Impact Explanation
NNS Governance uses `set_subnet_operational_level` with `DOWN_FOR_REPAIRS` to halt a CloudEngine subnet during subnet recovery or in response to a security incident. The Engine Controller's authorized caller — a lower-privileged canister developer role — can immediately call `engine_controller.update_subnet({subnet_id: X, is_halted: Some(false)})` to bring the subnet back online, overriding the Governance-level security decision. This undermines the integrity of the subnet recovery procedure: a subnet that Governance intentionally took offline can be brought back online by a lower-privileged actor before recovery is complete, potentially exposing the subnet to the same condition that triggered the halt.

### Likelihood Explanation
The Engine Controller's authorized caller is a specific principal with a defined operational role. The likelihood of exploitation depends on whether this principal acts contrary to NNS Governance's intent (e.g., due to key compromise, insider threat, or operational error). The design flaw is unconditional: there is no code path that prevents the override. The attack requires no special timing, no consensus manipulation, and no threshold corruption — only a single call from the Engine Controller's authorized principal.

### Recommendation
Add a check in `do_update_subnet` that prevents the Engine Controller from setting `is_halted = false` on a subnet whose current `is_halted = true` state was set by a Governance-level action. One approach: introduce a separate boolean field (e.g., `governance_halted`) in `SubnetRecord` that is only writable by Governance, and require `governance_halted == false` before the Engine Controller can set `is_halted = false`. Alternatively, disallow the Engine Controller from setting `is_halted = false` entirely, restricting it to only halting (not un-halting) subnets, with un-halting reserved for NNS Governance.

### Proof of Concept
1. NNS Governance passes a `SetSubnetOperationalLevel` proposal targeting a CloudEngine subnet with `operational_level = DOWN_FOR_REPAIRS`. The registry's `do_set_subnet_operational_level` writes `is_halted = true` to the subnet record.
2. The Engine Controller's authorized caller sends an ingress message to the Engine Controller canister: `update_subnet({subnet_id: <halted_cloud_engine_subnet>, is_halted: Some(false)})`.
3. The Engine Controller calls `registry.update_subnet` with the same payload.
4. `do_update_subnet` checks: caller is `ENGINE_CONTROLLER_CANISTER_ID` ✓, subnet type is `CloudEngine` ✓, only `is_halted` is set ✓ — no check on whether the current halt was set by Governance.
5. The registry writes `is_halted = false`, bringing the subnet back online and overriding NNS Governance's recovery decision. [6](#0-5) [11](#0-10)

### Citations

**File:** rs/registry/canister/canister/canister.rs (L137-144)
```rust
fn check_caller_is_governance_or_engine_controller_and_log(method_name: &str) {
    let caller = dfn_core::api::caller();
    println!("{LOG_PREFIX}call: {method_name} from: {caller}");
    assert!(
        caller == GOVERNANCE_CANISTER_ID.into() || caller == ENGINE_CONTROLLER_CANISTER_ID.into(),
        "{LOG_PREFIX}Principal: {caller} is not authorized to call this method: {method_name}"
    );
}
```

**File:** rs/registry/canister/canister/canister.rs (L871-884)
```rust
#[unsafe(export_name = "canister_update update_subnet")]
fn update_subnet() {
    check_caller_is_governance_or_engine_controller_and_log("update_subnet");
    over(candid_one, |payload: UpdateSubnetPayload| {
        update_subnet_(payload)
    });
}

#[candid_method(update, rename = "update_subnet")]
fn update_subnet_(payload: UpdateSubnetPayload) {
    let caller = dfn_core::api::caller();
    registry_mut().do_update_subnet(caller, payload);
    recertify_registry();
}
```

**File:** rs/registry/canister/canister/canister.rs (L1309-1318)
```rust
#[unsafe(export_name = "canister_update set_subnet_operational_level")]
fn set_subnet_operational_level() {
    check_caller_is_governance_and_log("set_subnet_operational_level");
    over(candid_one, set_subnet_operational_level_);
}

#[candid_method(update, rename = "set_subnet_operational_level")]
fn set_subnet_operational_level_(payload: SetSubnetOperationalLevelPayload) {
    registry_mut().do_set_subnet_operational_level(payload);
    recertify_registry();
```

**File:** rs/registry/canister/src/mutations/do_set_subnet_operational_level.rs (L44-76)
```rust
    pub fn do_set_subnet_operational_level(&mut self, payload: SetSubnetOperationalLevelPayload) {
        println!("{LOG_PREFIX}do_set_subnet_operational_level: {payload:?}");
        self.validate_set_subnet_operational_level(&payload)
            .unwrap();
        let SetSubnetOperationalLevelPayload {
            subnet_id,
            operational_level,
            ssh_readonly_access,
            ssh_node_state_write_access,
            recalled_replica_version_ids,
        } = payload;

        let mut mutations: Vec<RegistryMutation> = vec![];

        // Change SubnetRecord.
        if let Some(subnet_id) = subnet_id {
            mutations.push(modify_subnet_record_for_set_subnet_operational_level(
                subnet_id,
                self.get_subnet_or_panic(subnet_id),
                operational_level,
                ssh_readonly_access,
                recalled_replica_version_ids,
            ));
        }

        // Change NodeRecord(s).
        mutations.append(&mut modify_node_record_for_set_subnet_operational_level(
            ssh_node_state_write_access,
            |node_id| self.get_node_or_panic(node_id),
        ));

        self.maybe_apply_mutation_internal(mutations);
    }
```

**File:** rs/registry/canister/src/mutations/do_set_subnet_operational_level.rs (L225-233)
```rust
    if let Some(operational_level) = operational_level {
        let is_halted = match operational_level {
            operational_level::NORMAL => false,
            operational_level::DOWN_FOR_REPAIRS => true,
            _ => panic!("Unknown operational_level"),
        };

        subnet_record.is_halted = is_halted;
    }
```

**File:** rs/registry/canister/src/mutations/do_update_subnet.rs (L33-51)
```rust
    pub fn do_update_subnet(&mut self, caller: PrincipalId, payload: UpdateSubnetPayload) {
        println!("{LOG_PREFIX}do_update_subnet: caller={caller}, payload={payload:?}");

        let subnet_id = payload.subnet_id;

        // The engine controller canister is only allowed to mutate CloudEngine
        // subnets, and only a small subset of fields. Other authorized callers
        // (governance) can update any subnet and any field.
        if caller == ENGINE_CONTROLLER_CANISTER_ID.get() {
            let subnet_record = self.get_subnet_or_panic(subnet_id);
            assert_eq!(
                subnet_record.subnet_type,
                i32::from(SubnetTypePb::CloudEngine),
                "{LOG_PREFIX}do_update_subnet: engine controller may only update CloudEngine \
                 subnets; subnet {subnet_id} has subnet_type {:?}",
                subnet_record.subnet_type,
            );
            ensure_engine_controller_payload_scope(&payload);
        }
```

**File:** rs/registry/canister/src/mutations/do_update_subnet.rs (L246-252)
```rust
fn ensure_engine_controller_payload_scope(payload: &UpdateSubnetPayload) {
    let UpdateSubnetPayload {
        subnet_id: _,
        // The fields the engine controller is allowed to set.
        subnet_admins: _,
        is_halted: _,

```

**File:** rs/registry/canister/src/mutations/do_update_subnet.rs (L1894-1906)
```rust
    #[test]
    fn engine_controller_can_set_is_halted() {
        use ic_nns_constants::ENGINE_CONTROLLER_CANISTER_ID;

        let (mut registry, subnet_id) = make_registry_with_cloud_engine_subnet();

        let mut payload = make_empty_update_payload(subnet_id);
        payload.is_halted = Some(true);

        registry.do_update_subnet(ENGINE_CONTROLLER_CANISTER_ID.get(), payload);

        assert!(registry.get_subnet_or_panic(subnet_id).is_halted);
    }
```

**File:** rs/engine_controller/canister/canister.rs (L23-27)
```rust
/// The principal that is allowed to call this canister's methods when the
/// init/post-upgrade argument does not specify one.
const DEFAULT_AUTHORIZED_CALLER: &str =
    "bct5z-vccu4-6q4t2-3lb6l-wm43p-ulppt-o5sqq-w6het-rthdz-qp4yn-fqe";

```

**File:** rs/engine_controller/canister/canister.rs (L200-206)
```rust
fn ensure_only_allowed_fields_set(payload: &UpdateSubnetPayload) -> Result<(), String> {
    let UpdateSubnetPayload {
        subnet_id: _,
        // The fields we allow.
        subnet_admins: _,
        is_halted: _,

```

**File:** rs/engine_controller/canister/canister.rs (L318-341)
```rust
#[update]
async fn update_subnet(payload: UpdateSubnetPayload) -> Result<(), String> {
    ensure_authorized()?;
    ensure_only_allowed_fields_set(&payload)?;

    // Normalize `subnet_admins` so the super admin is always present.
    // The caller may omit the field entirely (no change requested), but if
    // they do supply one, we treat it as the source of truth and add the
    // super admin if missing.
    #[allow(unused_mut)]
    let mut payload = payload;
    if let Some(admins) = payload.subnet_admins {
        payload.subnet_admins = Some(normalize_subnet_admins(admins));
    }

    Call::unbounded_wait(REGISTRY_CANISTER_ID.into(), "update_subnet")
        .with_arg(payload)
        .await
        .map_err(|e| format!("registry.update_subnet call failed: {e:?}"))?
        .candid::<()>()
        .map_err(|e| format!("Failed to decode registry response: {e}"))?;

    Ok(())
}
```
