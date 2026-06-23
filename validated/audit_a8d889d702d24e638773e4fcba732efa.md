### Title
Missing Anonymous-Principal Validation on `AUTHORIZED_CALLER` Initialization Enables Privilege Escalation or Permanent Lockout - (File: rs/engine_controller/canister/canister.rs)

---

### Summary

The `apply_init_args` function in the engine controller canister sets `AUTHORIZED_CALLER` directly from the caller-supplied `EngineControllerInitArgs.authorized_caller` field without validating the value. If `Principal::anonymous()` is supplied, the `ensure_authorized()` guard passes for any unsigned ingress message, allowing any unprivileged sender to invoke `create_engine`, `delete_engine`, `update_subnet`, and `deploy_guestos_to_all_subnet_nodes`. Conversely, if an unreachable principal is supplied, all privileged functions are permanently locked — the direct IC analog of the Lido `owner = address(0)` lockout.

---

### Finding Description

`apply_init_args` (lines 66–81 of `rs/engine_controller/canister/canister.rs`) resolves the authorized caller and writes it unconditionally:

```rust
fn apply_init_args(args: Option<EngineControllerInitArgs>) {
    let args = args.unwrap_or_default();
    let authorized = args
        .authorized_caller
        .unwrap_or_else(default_authorized_caller);
    AUTHORIZED_CALLER.with(|c| *c.borrow_mut() = authorized);   // ← no validation
    ...
}
``` [1](#0-0) 

This function is called from both `#[init]` and `#[post_upgrade]`: [2](#0-1) 

The `EngineControllerInitArgs` struct imposes no constraints on `authorized_caller`:

```rust
pub struct EngineControllerInitArgs {
    pub authorized_caller: Option<Principal>,
    ...
}
``` [3](#0-2) 

The sole authorization gate for all privileged endpoints is `ensure_authorized()`, which performs a plain equality check:

```rust
fn ensure_authorized() -> Result<Principal, String> {
    let caller = msg_caller();
    let expected = AUTHORIZED_CALLER.with(|c| *c.borrow());
    if caller != expected {
        return Err(...);
    }
    Ok(caller)
}
``` [4](#0-3) 

Every privileged endpoint (`create_engine`, `delete_engine`, `update_subnet`, `deploy_guestos_to_all_subnet_nodes`) is gated exclusively by this check. [5](#0-4) 

**Scenario A — Authorization bypass (anonymous principal):**  
If `authorized_caller = Some(Principal::anonymous())` is passed at init or upgrade, `AUTHORIZED_CALLER` becomes the anonymous principal (`2vxsx-fae`). The IC protocol allows any user to send unsigned ingress messages, which arrive with `msg_caller() == Principal::anonymous()`. The equality check then passes for every such message, granting any unprivileged ingress sender full access to all privileged operations.

**Scenario B — Permanent lockout (unreachable principal):**  
If `authorized_caller` is set to a principal that can never originate an ingress message (e.g., a non-existent canister ID, the management canister `aaaaa-aa`, or a typo'd principal), `ensure_authorized()` will always return `Err`, permanently locking `create_engine`, `delete_engine`, `update_subnet`, and `deploy_guestos_to_all_subnet_nodes` — the direct IC analog of the Lido `owner = address(0)` lockout.

Additionally, `normalize_subnet_admins` reads `AUTHORIZED_CALLER` to inject the "super admin" into every subnet admin list: [6](#0-5) 

Under Scenario A, `Principal::anonymous()` would be injected as a subnet admin into every `update_subnet` call that modifies the admin list, propagating the misconfiguration into the registry.

---

### Impact Explanation

**Scenario A (anonymous principal):** Any unprivileged ingress sender can create new CloudEngine subnets, delete existing engine subnets, halt/unhalt subnets, update subnet admin lists, and trigger replica version deployments — all without any credentials. This is a complete governance authorization bypass for the engine controller's privileged surface.

**Scenario B (unreachable principal):** All four privileged endpoints are permanently inaccessible. The canister cannot be recovered without a controller-level upgrade, and if the controller itself is misconfigured, the lockout is irreversible.

---

### Likelihood Explanation

The trigger requires the canister's controller to supply a bad `authorized_caller` value at install or upgrade time — either by mistake (e.g., passing `Principal::anonymous()` during a scripted deployment) or through a compromised upgrade path. The `EngineControllerInitArgs` type provides no compile-time or runtime guard against this. The absence of any validation in `apply_init_args` means the misconfiguration is silently accepted and logged as legitimate. Given that the canister is explicitly designed to be reconfigured on every upgrade, the attack surface is exercised on every deployment.

---

### Recommendation

Add an explicit rejection of `Principal::anonymous()` (and optionally the management canister principal) inside `apply_init_args` before writing to `AUTHORIZED_CALLER`:

```rust
fn apply_init_args(args: Option<EngineControllerInitArgs>) {
    let args = args.unwrap_or_default();
    let authorized = args
        .authorized_caller
        .unwrap_or_else(default_authorized_caller);
    if authorized == Principal::anonymous() {
        ic_cdk::trap("authorized_caller must not be the anonymous principal");
    }
    AUTHORIZED_CALLER.with(|c| *c.borrow_mut() = authorized);
    ...
}
```

This mirrors the pattern already used elsewhere in the IC codebase (e.g., `check_anonymous_caller()` in the ckDOGE minter) and is the direct IC equivalent of the zero-address check recommended in the Lido report. [7](#0-6) 

---

### Proof of Concept

1. Install or upgrade the engine controller canister with:
   ```
   EngineControllerInitArgs {
       authorized_caller: Some(Principal::anonymous()),
       initial_dkg_subnet_id: None,
   }
   ```
2. Send an **unsigned** (anonymous) ingress update call to `create_engine` with any valid-looking `CreateEngineArgs`.
3. `msg_caller()` returns `Principal::anonymous()`; `AUTHORIZED_CALLER` is also `Principal::anonymous()`; `ensure_authorized()` returns `Ok(Principal::anonymous())`.
4. The call proceeds to forward a `CreateSubnetPayload` to the registry canister — subnet creation is triggered by an unprivileged sender with no credentials.

### Citations

**File:** rs/engine_controller/canister/canister.rs (L66-71)
```rust
fn apply_init_args(args: Option<EngineControllerInitArgs>) {
    let args = args.unwrap_or_default();
    let authorized = args
        .authorized_caller
        .unwrap_or_else(default_authorized_caller);
    AUTHORIZED_CALLER.with(|c| *c.borrow_mut() = authorized);
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

**File:** rs/engine_controller/canister/canister.rs (L104-107)
```rust
#[update]
async fn create_engine(args: CreateEngineArgs) -> Result<NewSubnet, String> {
    let caller = ensure_authorized()?;

```

**File:** rs/engine_controller/canister/canister.rs (L302-309)
```rust
fn normalize_subnet_admins(admins: Vec<PrincipalId>) -> Vec<PrincipalId> {
    let super_admin = PrincipalId(AUTHORIZED_CALLER.with(|c| *c.borrow()));
    let mut admins = admins;
    if !admins.contains(&super_admin) {
        admins.push(super_admin);
    }
    admins
}
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

**File:** rs/dogecoin/ckdoge/minter/src/main.rs (L145-149)
```rust
fn check_anonymous_caller() {
    if ic_cdk::api::msg_caller() == candid::Principal::anonymous() {
        panic!("anonymous caller not allowed")
    }
}
```
