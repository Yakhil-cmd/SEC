### Title
Centralized Single-Principal Governance Control Over Critical Subnet Operations - (File: rs/engine_controller/canister/canister.rs)

### Summary
The `engine_controller` canister gates all critical subnet lifecycle operations — `create_engine`, `delete_engine`, `update_subnet`, and `deploy_guestos_to_all_subnet_nodes` — behind a single hardcoded `AUTHORIZED_CALLER` principal. If the private key of that principal is compromised, an attacker gains unrestricted ability to create, delete, halt, and update CloudEngine subnets on the Internet Computer, with no secondary authorization layer, no emergency revocation path, and no multi-party approval requirement.

### Finding Description
The canister explicitly documents its design in the module comment:

> "Only a single, hard-coded authorized principal may invoke its methods." [1](#0-0) 

The default authorized principal is baked into the binary as `DEFAULT_AUTHORIZED_CALLER`: [2](#0-1) 

At runtime, `AUTHORIZED_CALLER` is a `thread_local` holding exactly one `Principal`. The `ensure_authorized()` guard performs a strict equality check — the caller must be exactly this one principal: [3](#0-2) 

Every privileged endpoint calls this guard:
- `create_engine` — creates a new CloudEngine subnet in the registry [4](#0-3) 
- `delete_engine` — deletes a subnet from the registry [5](#0-4) 
- `update_subnet` — halts/unhalts a subnet or changes its admin list [6](#0-5) 
- `deploy_guestos_to_all_subnet_nodes` — updates the replica version running on a subnet [7](#0-6) 

The `authorized_caller` can be overridden at `init`/`post_upgrade` time, but only by whoever controls the canister's upgrade path — not by the authorized caller itself. There is no on-chain mechanism for the authorized caller to rotate its own key, add a backup principal, or trigger an emergency halt without already holding the private key.

The `normalize_subnet_admins` function further entrenches this single principal by ensuring it is always injected into every subnet's admin list: [8](#0-7) 

The registry canister accepts calls from `ENGINE_CONTROLLER_CANISTER_ID` as a trusted source for `create_subnet`, `delete_subnet`, `update_subnet`, and `deploy_guestos_to_all_subnet_nodes`: [9](#0-8) 

### Impact Explanation
An attacker who obtains the private key of `bct5z-vccu4-6q4t2-3lb6l-wm43p-ulppt-o5sqq-w6het-rthdz-qp4yn-fqe` (or whatever principal is currently configured) can:

1. **Delete any CloudEngine subnet** via `delete_engine`, permanently removing it from the registry and halting all canisters on it.
2. **Halt any CloudEngine subnet** via `update_subnet` with `is_halted: true`, causing a denial-of-service for all users of that subnet.
3. **Replace subnet admins** via `update_subnet` with a malicious `subnet_admins` list, locking out legitimate operators.
4. **Deploy arbitrary (malicious) replica versions** to CloudEngine subnets via `deploy_guestos_to_all_subnet_nodes`, enabling code execution at the replica level.

These are governance authorization bugs with direct, irreversible protocol-level impact on subnet availability and integrity.

### Likelihood Explanation
The authorized caller is a self-authenticating principal whose security depends entirely on the secrecy of a single private key. The key must be used operationally (to call the canister), meaning it cannot be kept in cold storage indefinitely. There is no threshold/multi-sig requirement, no time-lock, and no on-chain revocation mechanism. A single key compromise — through operational security failure, insider threat, or infrastructure breach — is sufficient to trigger the full impact.

### Recommendation
1. **Multi-principal authorization**: Replace the single `AUTHORIZED_CALLER` with a set of authorized principals, requiring M-of-N agreement (e.g., via a proposal-based flow or an on-chain multi-sig canister) for destructive operations like `delete_engine` and `deploy_guestos_to_all_subnet_nodes`.
2. **NNS governance integration**: Route critical operations through NNS governance proposals rather than a single off-chain key, consistent with how other registry mutations are authorized.
3. **Emergency halt capability**: Introduce a separate, independently-held emergency-stop principal (or NNS-controlled flag) that can freeze the engine controller without requiring the operational key.
4. **Key rotation endpoint**: Add an on-chain method, callable only by the current authorized caller or the canister's NNS controller, to rotate `AUTHORIZED_CALLER` without requiring a full canister upgrade.
5. **Audit logging**: Emit certified log entries for every privileged action so that unauthorized use is detectable even after the fact.

### Proof of Concept
An attacker with the private key of `DEFAULT_AUTHORIZED_CALLER` sends the following ingress message to the engine controller canister:

```
dfx canister --network ic call <ENGINE_CONTROLLER_CANISTER_ID> delete_engine \
  '(record { subnet_id = principal "<target_cloud_engine_subnet_id>" })'
```

The `ensure_authorized()` check at line 93–102 passes because `msg_caller()` equals `AUTHORIZED_CALLER`. The call is forwarded to `registry.delete_subnet` at line 180. The registry's `check_caller_is_governance_or_engine_controller_and_log` at line 141 of `rs/registry/canister/canister/canister.rs` passes because the caller is `ENGINE_CONTROLLER_CANISTER_ID`. The subnet is permanently deleted from the registry with no further approval required. [10](#0-9) [11](#0-10) [9](#0-8)

### Citations

**File:** rs/engine_controller/canister/canister.rs (L1-5)
```rust
//! The engine controller canister.
//!
//! This canister provides a thin user-facing API on top of the registry
//! canister's `create_subnet` / `delete_subnet` endpoints. Only a single,
//! hard-coded authorized principal may invoke its methods.
```

**File:** rs/engine_controller/canister/canister.rs (L23-26)
```rust
/// The principal that is allowed to call this canister's methods when the
/// init/post-upgrade argument does not specify one.
const DEFAULT_AUTHORIZED_CALLER: &str =
    "bct5z-vccu4-6q4t2-3lb6l-wm43p-ulppt-o5sqq-w6het-rthdz-qp4yn-fqe";
```

**File:** rs/engine_controller/canister/canister.rs (L44-47)
```rust
thread_local! {
    /// The principal currently allowed to call the canister's methods. Set on
    /// `init` and re-evaluated on every `post_upgrade`.
    static AUTHORIZED_CALLER: RefCell<Principal> = RefCell::new(default_authorized_caller());
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

**File:** rs/engine_controller/canister/canister.rs (L104-106)
```rust
#[update]
async fn create_engine(args: CreateEngineArgs) -> Result<NewSubnet, String> {
    let caller = ensure_authorized()?;
```

**File:** rs/engine_controller/canister/canister.rs (L171-188)
```rust
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

**File:** rs/engine_controller/canister/canister.rs (L302-308)
```rust
fn normalize_subnet_admins(admins: Vec<PrincipalId>) -> Vec<PrincipalId> {
    let super_admin = PrincipalId(AUTHORIZED_CALLER.with(|c| *c.borrow()));
    let mut admins = admins;
    if !admins.contains(&super_admin) {
        admins.push(super_admin);
    }
    admins
```

**File:** rs/engine_controller/canister/canister.rs (L318-320)
```rust
#[update]
async fn update_subnet(payload: UpdateSubnetPayload) -> Result<(), String> {
    ensure_authorized()?;
```

**File:** rs/engine_controller/canister/canister.rs (L345-349)
```rust
#[update]
async fn deploy_guestos_to_all_subnet_nodes(
    payload: DeployGuestosToAllSubnetNodesPayload,
) -> Result<(), String> {
    ensure_authorized()?;
```

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
