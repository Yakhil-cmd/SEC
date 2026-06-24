### Title
Single-Step Authorized Caller Update Can Brick Engine Controller - (File: rs/engine_controller/canister/canister.rs)

---

### Summary
The `engine_controller` canister stores a single `AUTHORIZED_CALLER` principal that gates every business method. This principal is set atomically during `init`/`post_upgrade` with no two-step confirmation. If the wrong principal is supplied in `EngineControllerInitArgs.authorized_caller` during an upgrade, every business method becomes permanently inaccessible until a corrective upgrade is executed — which may itself require an NNS governance proposal.

---

### Finding Description
`AUTHORIZED_CALLER` is a `thread_local!` `RefCell<Principal>` initialized by `apply_init_args`, which is called unconditionally from both `#[init]` and `#[post_upgrade]`:

```rust
// rs/engine_controller/canister/canister.rs  lines 66-81
fn apply_init_args(args: Option<EngineControllerInitArgs>) {
    let args = args.unwrap_or_default();
    let authorized = args
        .authorized_caller
        .unwrap_or_else(default_authorized_caller);
    AUTHORIZED_CALLER.with(|c| *c.borrow_mut() = authorized);
    ...
}
```

Every exported update method is gated by `ensure_authorized()`:

```rust
// lines 93-102
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

The canister exposes **no runtime method** to update `AUTHORIZED_CALLER`. The only mutation path is a canister upgrade carrying a new `EngineControllerInitArgs`. If `authorized_caller` is set to a wrong or inaccessible principal during an upgrade, the canister is immediately bricked: `create_engine`, `delete_engine`, `update_subnet`, and `deploy_guestos_to_all_subnet_nodes` all reject every caller.

There is no pending-state mechanism, no acceptance step by the new principal, and no fallback.

---

### Impact Explanation
**High.** The engine controller is the sole interface for creating, deleting, and updating Cloud Engine subnets. If `AUTHORIZED_CALLER` is set to an unreachable principal, none of those operations can proceed. Engine subnets cannot be created or decommissioned, and halting/unhalting them via `update_subnet` is also blocked. Recovery requires a new upgrade, which — if the NNS governance canister is the controller — demands a full NNS proposal cycle (days of delay).

---

### Likelihood Explanation
**Low.** The mistake must occur during a deliberate upgrade operation performed by the canister's controller. It requires a typo or copy-paste error in the `authorized_caller` field of `EngineControllerInitArgs`. This is the same low-likelihood, high-impact profile as the reference report.

---

### Recommendation
Adopt a two-step authorized-caller rotation:

1. Add a `propose_authorized_caller(new: Principal)` update method (callable only by the current `AUTHORIZED_CALLER`) that writes `new` into a `PENDING_AUTHORIZED_CALLER` cell.
2. Add an `accept_authorized_caller()` update method (callable only by `PENDING_AUTHORIZED_CALLER`) that promotes the pending value to `AUTHORIZED_CALLER`.

Until the new principal calls `accept_authorized_caller`, the old principal retains full control and can cancel the rotation. This eliminates the single-point-of-failure window.

---

### Proof of Concept

1. Current state: `AUTHORIZED_CALLER = A` (the legitimate operator principal).
2. The canister controller upgrades the canister with:
   ```
   EngineControllerInitArgs { authorized_caller: Some(wrong_principal), ... }
   ```
3. `post_upgrade` → `apply_init_args` → `AUTHORIZED_CALLER.with(|c| *c.borrow_mut() = wrong_principal)`. [1](#0-0) 
4. Principal `A` calls `create_engine(...)`. `ensure_authorized()` computes `msg_caller() == A ≠ wrong_principal` and returns `Err("Caller A is not authorized …")`. [2](#0-1) 
5. All four exported update methods (`create_engine`, `delete_engine`, `update_subnet`, `deploy_guestos_to_all_subnet_nodes`) are now permanently inaccessible to the legitimate operator. [3](#0-2) 
6. Recovery requires a new upgrade — potentially gated behind an NNS governance proposal — introducing a multi-day outage window for all engine-subnet lifecycle operations. [4](#0-3)

### Citations

**File:** rs/engine_controller/canister/canister.rs (L66-81)
```rust
fn apply_init_args(args: Option<EngineControllerInitArgs>) {
    let args = args.unwrap_or_default();
    let authorized = args
        .authorized_caller
        .unwrap_or_else(default_authorized_caller);
    AUTHORIZED_CALLER.with(|c| *c.borrow_mut() = authorized);
    let initial_dkg_subnet_id = args
        .initial_dkg_subnet_id
        .map(|p| SubnetId::new(PrincipalId(p)))
        .unwrap_or_else(default_initial_dkg_subnet_id);
    INITIAL_DKG_SUBNET_ID.with(|c| *c.borrow_mut() = initial_dkg_subnet_id);
    println!(
        "engine_controller: authorized caller set to {authorized}, \
         initial_dkg_subnet_id set to {initial_dkg_subnet_id}"
    );
}
```

**File:** rs/engine_controller/canister/canister.rs (L88-91)
```rust
#[post_upgrade]
fn post_upgrade(args: Option<EngineControllerInitArgs>) {
    apply_init_args(args);
}
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

**File:** rs/engine_controller/canister/canister.rs (L104-188)
```rust
#[update]
async fn create_engine(args: CreateEngineArgs) -> Result<NewSubnet, String> {
    let caller = ensure_authorized()?;

    // Validate node list.
    if args.node_ids.len() < REQUIRED_NODE_COUNT {
        return Err(format!(
            "Expected at least {REQUIRED_NODE_COUNT} node ids, got {}",
            args.node_ids.len()
        ));
    }
    let mut seen: HashSet<Principal> = HashSet::new();
    for n in &args.node_ids {
        if !seen.insert(*n) {
            return Err(format!("Duplicate node id supplied: {n}"));
        }
    }

    // Make sure the caller is part of the subnet admins.
    let mut subnet_admins: Vec<PrincipalId> =
        args.subnet_admins.into_iter().map(PrincipalId).collect();
    let caller_pid = PrincipalId(caller);
    if !subnet_admins.contains(&caller_pid) {
        subnet_admins.push(caller_pid);
    }

    let node_ids: Vec<NodeId> = args
        .node_ids
        .into_iter()
        .map(|p| NodeId::from(PrincipalId(p)))
        .collect();

    let initial_dkg_subnet_id = INITIAL_DKG_SUBNET_ID.with(|c| *c.borrow());

    let payload = CreateSubnetPayload {
        node_ids,
        subnet_admins: Some(subnet_admins),
        replica_version_id: args.replica_version_id,
        subnet_type: SubnetType::CloudEngine,
        initial_dkg_subnet_id: Some(initial_dkg_subnet_id),
        dkg_interval_length: 499,
        dkg_dealings_per_block: 1,
        initial_notary_delay_millis: 300,
        max_block_payload_size: 4 * 1024 * 1024, // 4 MiB
        max_ingress_bytes_per_message: 2 * 1024 * 1024, // 2 MiB
        max_ingress_messages_per_block: 1000,
        unit_delay_millis: 1000,
        canister_cycles_cost_schedule: Some(CanisterCyclesCostSchedule::Free),
        features: SubnetFeatures {
            canister_sandboxing: false,
            http_requests: true,
            sev_enabled: Some(false),
        },
        ..Default::default()
    };

    let response: Result<NewSubnet, String> =
        Call::unbounded_wait(REGISTRY_CANISTER_ID.into(), "create_subnet")
            .with_arg(payload)
            .await
            .map_err(|e| format!("registry.create_subnet call failed: {e:?}"))?
            .candid()
            .map_err(|e| format!("Failed to decode registry response: {e}"))?;

    response
}

#[update]
async fn delete_engine(args: DeleteEngineArgs) -> Result<(), String> {
    ensure_authorized()?;

    let payload = DeleteSubnetPayload {
        subnet_id: args.subnet_id,
    };

    let response: Result<(), String> =
        Call::unbounded_wait(REGISTRY_CANISTER_ID.into(), "delete_subnet")
            .with_arg(payload)
            .await
            .map_err(|e| format!("registry.delete_subnet call failed: {e:?}"))?
            .candid()
            .map_err(|e| format!("Failed to decode registry response: {e}"))?;

    response
}
```
