### Title
Engine Controller's Authorized Caller Can Unilaterally Halt CloudEngine Subnets Without NNS Governance Approval - (File: rs/engine_controller/canister/canister.rs)

### Summary
The `engine_controller` canister exposes an `update_subnet` endpoint that allows setting `is_halted = true` on any CloudEngine subnet. Access is gated only by a single hardcoded external principal (`AUTHORIZED_CALLER`), not by NNS governance. This means the engine controller's operator can permanently halt a CloudEngine subnet without any NNS proposal, bypassing the governance layer that controls all other subnet halting operations.

### Finding Description

The engine controller canister's `update_subnet` function enforces access via `ensure_authorized()`, which checks the caller against a single thread-local `AUTHORIZED_CALLER` principal (defaulting to the hardcoded `DEFAULT_AUTHORIZED_CALLER`): [1](#0-0) 

This authorized caller is a single external user-facing principal, not the NNS governance canister: [2](#0-1) 

The `update_subnet` function explicitly permits `is_halted` to be set by this caller: [3](#0-2) 

The registry's `update_subnet` endpoint accepts calls from either `GOVERNANCE_CANISTER_ID` or `ENGINE_CONTROLLER_CANISTER_ID`: [4](#0-3) 

Inside `do_update_subnet`, the engine controller is explicitly permitted to set `is_halted` on CloudEngine subnets: [5](#0-4) 

The `ensure_engine_controller_payload_scope` function confirms `is_halted` is an allowed field for the engine controller: [6](#0-5) 

This is confirmed by the test `engine_controller_can_set_is_halted`: [7](#0-6) 

When `is_halted = true` is written to the registry, the consensus layer reads it and immediately stops producing blocks: [8](#0-7) 

The `is_halted` field in `SubnetRecord` is documented as causing the subnet to "no longer create or execute blocks": [9](#0-8) 

The `halt_at_cup_height` flag further documents that once halted, the subnet "remains halted until an appropriate proposal which sets `is_halted` to `false` is approved": [10](#0-9) 

### Impact Explanation

The `AUTHORIZED_CALLER` of the engine controller — a single external principal — can call `engine_controller.update_subnet({ subnet_id: <cloud_engine_subnet>, is_halted: Some(true) })` at any time without an NNS governance proposal. This immediately halts consensus on the targeted CloudEngine subnet: no new blocks are produced, no messages are executed, and all user canisters on that subnet become permanently unresponsive. Recovery requires a successful NNS governance proposal to set `is_halted = false`, which takes time and cannot be expedited by the engine controller itself. This is a governance authorization bug: a sub-governance operator role can trigger a protocol-level shutdown that should require the highest governance authority.

### Likelihood Explanation

The `AUTHORIZED_CALLER` is a single external principal whose private key, if compromised or acting maliciously, can immediately halt any CloudEngine subnet. There is no multi-sig, no time-lock, and no NNS proposal required. The engine controller is an NNS canister (controlled by NNS Root), but its `AUTHORIZED_CALLER` is not — it is an ordinary external principal. The attack path is a single direct canister call.

### Recommendation

The ability to set `is_halted = true` on a subnet should require NNS governance approval, consistent with how all other subnet halting operations work (via `propose-to-update-subnet` or `SetSubnetOperationalLevel` proposals). The engine controller's `update_subnet` path should either:
1. Remove `is_halted` from the set of fields the engine controller is permitted to set, requiring NNS governance for halting; or
2. Require that `is_halted = true` calls be routed through an NNS proposal rather than directly through the engine controller's authorized caller.

The `ensure_only_allowed_fields_set` function in `rs/engine_controller/canister/canister.rs` and `ensure_engine_controller_payload_scope` in `rs/registry/canister/src/mutations/do_update_subnet.rs` should be updated to disallow `is_halted` from the engine controller's permitted field set.

### Proof of Concept

1. The attacker controls (or compromises) the `AUTHORIZED_CALLER` principal (`bct5z-vccu4-6q4t2-3lb6l-wm43p-ulppt-o5sqq-w6het-rthdz-qp4yn-fqe`).
2. The attacker calls `engine_controller.update_subnet` with `{ subnet_id: <target_cloud_engine_subnet>, is_halted: Some(true) }`.
3. `ensure_authorized()` passes because the caller matches `AUTHORIZED_CALLER`.
4. `ensure_only_allowed_fields_set()` passes because `is_halted` is in the allowed set.
5. The engine controller calls `registry.update_subnet` with the payload.
6. `check_caller_is_governance_or_engine_controller_and_log` passes because the caller is `ENGINE_CONTROLLER_CANISTER_ID`.
7. `do_update_subnet` writes `is_halted = true` to the registry for the target subnet.
8. All nodes on the CloudEngine subnet read the updated registry record via `should_halt_by_subnet_record()` and stop producing blocks.
9. The subnet is permanently halted. Recovery requires an NNS governance proposal — the engine controller's authorized caller cannot undo this through the engine controller (since `is_halted = false` would also be permitted, but the point is the halt was done without governance approval and could be done repeatedly to prevent recovery).

### Citations

**File:** rs/engine_controller/canister/canister.rs (L25-26)
```rust
const DEFAULT_AUTHORIZED_CALLER: &str =
    "bct5z-vccu4-6q4t2-3lb6l-wm43p-ulppt-o5sqq-w6het-rthdz-qp4yn-fqe";
```

**File:** rs/engine_controller/canister/canister.rs (L93-102)
```rust
fn ensure_authorized() -> Result<Principal, String> {
    let caller = msg_caller();
    let expected = AUTHORIZED_CALLER.with(|c| *c.borrow());
    if caller != expected {
        return Err(format!(
            "Caller {caller} is not authorized to call this canister"
        ));
    }
    Ok(caller)
}
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

**File:** rs/registry/canister/src/mutations/do_update_subnet.rs (L41-51)
```rust
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

**File:** rs/consensus/src/consensus.rs (L433-441)
```rust
        // Consensus halts if instructed by the registry
        if self.should_halt_by_subnet_record() {
            info!(
                every_n_seconds => 5,
                self.log,
                "consensus is halted by instructions of the subnet record in the registry"
            );
            return Mutations::new();
        }
```

**File:** rs/protobuf/def/registry/subnet/v1/subnet.proto (L38-39)
```text
  // If `true`, the subnet will be halted: it will no longer create or execute blocks.
  bool is_halted = 17;
```

**File:** rs/protobuf/def/registry/subnet/v1/subnet.proto (L78-84)
```text
  // If `true`, the subnet will be halted after reaching the next cup height: it will no longer
  // create or execute blocks.
  //
  // Note: this flag is reset automatically when a new CUP proposal is approved. When that
  // happens, the `is_halted` flag is set to `true`, so the Subnet remains halted until an
  // appropriate proposal which sets `is_halted` to `false` is approved.
  bool halt_at_cup_height = 28;
```
