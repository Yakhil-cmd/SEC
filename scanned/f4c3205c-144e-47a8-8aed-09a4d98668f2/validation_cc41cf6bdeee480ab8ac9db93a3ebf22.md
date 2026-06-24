### Title
Hardcoded `DEFAULT_AUTHORIZED_CALLER` in Engine Controller Canister Silently Restores Unrestricted Subnet Lifecycle Privileges on Every Upgrade Without Explicit Args — (`rs/engine_controller/canister/canister.rs`)

---

### Summary

The engine controller canister (`si2b5-pyaaa-aaaaa-aaaja-cai`) hardcodes a single principal (`bct5z-vccu4-6q4t2-3lb6l-wm43p-ulppt-o5sqq-w6het-rthdz-qp4yn-fqe`) as `DEFAULT_AUTHORIZED_CALLER`. This principal holds unconditional authority to create, delete, update, and deploy replica versions to CloudEngine subnets. Critically, every `post_upgrade` invocation that omits `authorized_caller` silently restores this hardcoded principal's full control — even if the NNS governance had previously overridden it. The NNS cannot permanently revoke this access without a code change and a new WASM deployment.

---

### Finding Description

The constant is baked into the WASM binary: [1](#0-0) 

The `apply_init_args` function, called on both `init` and `post_upgrade`, falls back to this hardcoded principal whenever `authorized_caller` is absent: [2](#0-1) 

Both lifecycle hooks call this function unconditionally: [3](#0-2) 

The `ensure_authorized` guard grants this principal full access to every sensitive endpoint: [4](#0-3) 

Those endpoints proxy directly to the registry canister's `create_subnet`, `delete_subnet`, `update_subnet`, and `deploy_guestos_to_all_subnet_nodes` — all of which are now also callable by the engine controller canister ID (`si2b5-pyaaa-aaaaa-aaaja-cai`): [5](#0-4) 

The integration test explicitly documents and confirms the silent-restore behavior: upgrading with `None` args causes the hardcoded default to become authorized again, even after a custom caller was set: [6](#0-5) 

Additionally, `normalize_subnet_admins` permanently injects the `AUTHORIZED_CALLER` (which defaults to the hardcoded principal) into every CloudEngine subnet's admin list on every `update_subnet` call, making the hardcoded principal a persistent subnet admin even at the registry level: [7](#0-6) 

---

### Impact Explanation

The hardcoded principal holds permanent, unconditional authority to:

- **Create** new CloudEngine subnets (adding them to the IC topology)
- **Delete** CloudEngine subnets (removing them from the IC topology)
- **Update** subnet admins and halt/unhalt CloudEngine subnets
- **Deploy** arbitrary blessed replica versions to all nodes of CloudEngine subnets

Because `post_upgrade` resets `AUTHORIZED_CALLER` to the hardcoded default whenever `authorized_caller` is omitted from upgrade args, the NNS governance cannot permanently revoke this access. A routine NNS upgrade proposal that does not explicitly pass `authorized_caller` silently restores the hardcoded principal's full control — a non-obvious behavior that creates a persistent elevated-privilege principal outside NNS governance's permanent control.

Furthermore, `normalize_subnet_admins` ensures the hardcoded principal is injected into every CloudEngine subnet's admin list at the registry level on every `update_subnet` call, compounding the persistence of this access. [8](#0-7) 

---

### Likelihood Explanation

**Medium.** The NNS governance controls upgrades of the engine controller canister. Any NNS upgrade proposal that omits `authorized_caller` in the upgrade args — which is the default/easy path, as the `EngineControllerInitArgs` struct has `authorized_caller: Option<Principal>` defaulting to `None` — silently restores the hardcoded principal's full control. This is a realistic operational mistake. Additionally, if the private key for `bct5z-...` is ever compromised, the attacker immediately gains full CloudEngine subnet lifecycle control with no on-chain revocation mechanism available to the NNS short of a code change. [9](#0-8) 

---

### Recommendation

1. **Persist the authorized caller in stable memory** so it survives upgrades without needing to be re-specified on every `post_upgrade`. The current `thread_local!` storage is ephemeral and resets on every upgrade.
2. **Require explicit `authorized_caller` in upgrade args** — reject upgrades that omit it, or at minimum emit a prominent warning log when the hardcoded default is applied.
3. **Remove the hardcoded default entirely** and require the NNS to always specify the authorized caller at install/upgrade time, making the access fully governance-controlled.
4. **Do not inject the hardcoded principal into subnet admin lists** via `normalize_subnet_admins` — this creates registry-level persistence of the hardcoded principal's access that outlives the canister's own state.

---

### Proof of Concept

1. NNS governance upgrades the engine controller with `authorized_caller: Some(new_principal)` — `new_principal` is now the sole authorized caller; the hardcoded `bct5z-...` is rejected.
2. NNS governance later issues a routine upgrade with `authorized_caller: None` (or omits the field entirely, which is the `Default` behavior of `EngineControllerInitArgs`).
3. `apply_init_args` falls back to `default_authorized_caller()`, silently restoring `bct5z-...` as the sole authorized caller.
4. The hardcoded principal can now call `create_engine` (creating new subnets), `delete_engine` (deleting CloudEngine subnets), `update_subnet` (modifying subnet admins, halting subnets), and `deploy_guestos_to_all_subnet_nodes` (deploying replica versions to all nodes) — all without any NNS governance approval for individual actions. [10](#0-9) [11](#0-10) [12](#0-11) [13](#0-12) [14](#0-13) [15](#0-14)

### Citations

**File:** rs/engine_controller/canister/canister.rs (L23-26)
```rust
/// The principal that is allowed to call this canister's methods when the
/// init/post-upgrade argument does not specify one.
const DEFAULT_AUTHORIZED_CALLER: &str =
    "bct5z-vccu4-6q4t2-3lb6l-wm43p-ulppt-o5sqq-w6het-rthdz-qp4yn-fqe";
```

**File:** rs/engine_controller/canister/canister.rs (L55-58)
```rust
fn default_authorized_caller() -> Principal {
    Principal::from_text(DEFAULT_AUTHORIZED_CALLER)
        .expect("hardcoded DEFAULT_AUTHORIZED_CALLER must be a valid principal")
}
```

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

**File:** rs/engine_controller/canister/canister.rs (L83-91)
```rust
#[init]
fn init(args: Option<EngineControllerInitArgs>) {
    apply_init_args(args);
}

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

**File:** rs/engine_controller/canister/canister.rs (L104-106)
```rust
#[update]
async fn create_engine(args: CreateEngineArgs) -> Result<NewSubnet, String> {
    let caller = ensure_authorized()?;
```

**File:** rs/engine_controller/canister/canister.rs (L171-173)
```rust
#[update]
async fn delete_engine(args: DeleteEngineArgs) -> Result<(), String> {
    ensure_authorized()?;
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

**File:** rs/engine_controller/tests/tests.rs (L416-432)
```rust
    // Upgrade with no override: default principal becomes authorized again.
    pic.upgrade_canister(
        ENGINE_CONTROLLER_CANISTER_ID.into(),
        wasm.bytes(),
        Encode!(&None::<EngineControllerInitArgs>).unwrap(),
        Some(Principal::anonymous()),
    )
    .await
    .expect("upgrade should succeed");

    let err = call_create_engine(&pic, custom_caller, &args)
        .await
        .unwrap_err();
    assert!(
        err.contains("not authorized"),
        "custom caller must be rejected after upgrade with default: {err}"
    );
```

**File:** rs/registry/canister/unreleased_changelog.md (L30-39)
```markdown
* The `create_subnet` and `delete_subnet` endpoints can now be called by the
  engine controller canister (`si2b5-pyaaa-aaaaa-aaaja-cai`) in addition to the
  governance canister.
* The `update_subnet` and `deploy_guestos_to_all_subnet_nodes` endpoints can now
  also be called by the engine controller canister
  (`si2b5-pyaaa-aaaaa-aaaja-cai`) in addition to the governance canister. When
  invoked by the engine controller, both endpoints are restricted to acting on
  `CloudEngine` subnets only — any attempt to target a subnet of a different
  type is rejected. Calls from the governance canister are unaffected and may
  still target subnets of any type.
```

**File:** rs/engine_controller/src/lib.rs (L19-28)
```rust
#[derive(Clone, Debug, Default, CandidType, Deserialize)]
pub struct EngineControllerInitArgs {
    /// If `Some`, replaces the default authorized caller; if `None`, the
    /// default is kept.
    pub authorized_caller: Option<Principal>,
    /// If `Some`, replaces the default `initial_dkg_subnet_id` used when
    /// forwarding `CreateSubnetPayload` to the registry; if `None`, the
    /// hard-coded default is kept.
    pub initial_dkg_subnet_id: Option<Principal>,
}
```
