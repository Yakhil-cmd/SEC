The critical piece I needed: `rs/messaging/src/message_routing.rs` confirms that `subnet_admins` is read from the registry and loaded into `SubnetTopology` **at the protocol level** for CloudEngine subnets. This makes the analog concrete and exploitable.

---

### Title
Stale Subnet Admin After Engine Controller Authorized-Caller Rotation — (`rs/engine_controller/canister/canister.rs`)

### Summary
When the engine controller canister's `AUTHORIZED_CALLER` is rotated via `post_upgrade`, the old authorized caller is **not removed** from the `subnet_admins` list of any previously created CloudEngine subnets in the registry. Because the message routing layer reads `subnet_admins` from the registry and loads them into `SubnetTopology` for protocol-level enforcement, the old caller retains subnet-admin privileges on every CloudEngine subnet it helped create, even after it has been revoked from the engine controller itself.

### Finding Description

**Step 1 — Admin set at creation time.**
`create_engine` in `rs/engine_controller/canister/canister.rs` unconditionally inserts the current `AUTHORIZED_CALLER` into the `subnet_admins` list that is forwarded to the registry's `create_subnet` endpoint:

```rust
let caller_pid = PrincipalId(caller);
if !subnet_admins.contains(&caller_pid) {
    subnet_admins.push(caller_pid);
}
// ...
let payload = CreateSubnetPayload {
    subnet_admins: Some(subnet_admins),
    ...
};
``` [1](#0-0) 

**Step 2 — Authorized caller can be rotated via upgrade.**
`post_upgrade` calls `apply_init_args`, which overwrites `AUTHORIZED_CALLER` in the canister's thread-local state. The new principal becomes the sole entity allowed to call `create_engine`, `delete_engine`, `update_subnet`, and `deploy_guestos_to_all_subnet_nodes`. [2](#0-1) 

**Step 3 — `normalize_subnet_admins` only adds, never removes.**
When `update_subnet` is called after a rotation, `normalize_subnet_admins` ensures the **new** `AUTHORIZED_CALLER` is present in the admin list, but it never evicts the old one. If `update_subnet` is not called for a given subnet (or is called with `subnet_admins: None`), the old caller remains in the registry record indefinitely. [3](#0-2) 

**Step 4 — `subnet_admins` is enforced at the protocol level.**
The message routing layer reads `subnet_admins` from the registry for every CloudEngine subnet and populates `SubnetTopology`:

```rust
|| (subnet_type == SubnetType::CloudEngine
    && cost_schedule == CanisterCyclesCostSchedule::Free)
{
    for p in subnet_record.subnet_admins.into_iter() {
        ...
        subnet_admins.insert(subnet_admin);
    }
}
``` [4](#0-3) 

The `subnet_admins` set in `SubnetTopology` is then used by the replica for protocol-level access control decisions on that subnet. [5](#0-4) 

### Impact Explanation
After the engine controller is upgraded with a new `authorized_caller` (principal B), the old authorized caller (principal A) is blocked from calling any engine controller endpoint. However, A remains listed as a subnet admin in the registry for every CloudEngine subnet it created. The message routing layer reads this stale entry and grants A protocol-level subnet-admin privileges on those subnets — privileges that the new owner (B) and the system operators believe have been revoked. Depending on what subnet-admin status permits at the replica level (e.g., halting, management-canister interactions scoped to admins), A can continue to exercise those powers without going through the engine controller.

### Likelihood Explanation
The engine controller is explicitly designed to support `authorized_caller` rotation via `post_upgrade` with an `EngineControllerInitArgs` argument. Key rotation and ownership transfer are normal operational events. Any such rotation silently leaves all previously created CloudEngine subnets with a stale admin entry. No special attacker capability is required beyond having previously been the authorized caller.

### Recommendation
When `post_upgrade` changes `AUTHORIZED_CALLER` from old principal A to new principal B, the engine controller should iterate over all CloudEngine subnets it manages and call `update_subnet` with a `subnet_admins` payload that removes A and ensures B is present. Alternatively, `normalize_subnet_admins` should be extended to also remove the **previous** `AUTHORIZED_CALLER` from the list whenever the admin set is updated.

### Proof of Concept
1. Deploy the engine controller with `AUTHORIZED_CALLER = A`.
2. Call `create_engine(...)` as A. The registry now records `subnet_admins = [A]` for the new CloudEngine subnet S.
3. Upgrade the engine controller with `EngineControllerInitArgs { authorized_caller: Some(B), ... }`. `AUTHORIZED_CALLER` is now B; A is rejected by `ensure_authorized()`.
4. No `update_subnet` call is made for S (or it is made with `subnet_admins: None`).
5. The registry still holds `subnet_admins = [A]` for subnet S.
6. The message routing layer reads this record and includes A in `SubnetTopology.subnet_admins` for S.
7. A retains protocol-level subnet-admin privileges on S despite having been revoked from the engine controller.

### Citations

**File:** rs/engine_controller/canister/canister.rs (L66-91)
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

#[init]
fn init(args: Option<EngineControllerInitArgs>) {
    apply_init_args(args);
}

#[post_upgrade]
fn post_upgrade(args: Option<EngineControllerInitArgs>) {
    apply_init_args(args);
}
```

**File:** rs/engine_controller/canister/canister.rs (L122-140)
```rust
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
```

**File:** rs/engine_controller/canister/canister.rs (L299-309)
```rust
/// Ensures that the configured `AUTHORIZED_CALLER` (the engine controller's
/// "super admin") is always present in the resulting admin list, even if the
/// caller forgot to include it.
fn normalize_subnet_admins(admins: Vec<PrincipalId>) -> Vec<PrincipalId> {
    let super_admin = PrincipalId(AUTHORIZED_CALLER.with(|c| *c.borrow()));
    let mut admins = admins;
    if !admins.contains(&super_admin) {
        admins.push(super_admin);
    }
    admins
}
```

**File:** rs/messaging/src/message_routing.rs (L1024-1035)
```rust
                && cost_schedule == CanisterCyclesCostSchedule::Free)
                || (subnet_type == SubnetType::CloudEngine
                    && cost_schedule == CanisterCyclesCostSchedule::Free)
            {
                for p in subnet_record.subnet_admins.into_iter() {
                    let subnet_admin = PrincipalId::try_from(p).map_err(|err| {
                        ReadRegistryError::Persistent(format!(
                            "'failed to read subnet admins from subnet record', err: {err:?}"
                        ))
                    })?;
                    subnet_admins.insert(subnet_admin);
                }
```

**File:** rs/protobuf/def/registry/subnet/v1/subnet.proto (L95-96)
```text
  // List of principals that have admin privileges on the subnet.
  repeated types.v1.PrincipalId subnet_admins = 31;
```
