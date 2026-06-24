### Title
No Runtime Mechanism to Remove Individual Principals from Migration Canister Allowlist — (File: `rs/migration_canister/src/canister_state.rs`)

---

### Summary

The migration canister uses an `ALLOWLIST` to gate access to `migrate_canister` during soft rollout. However, there is no runtime endpoint to add or remove individual principals from this list. The only way to modify the allowlist is via a full canister upgrade. Additionally, the `ALLOWLIST` is stored in non-stable `thread_local!` memory, meaning it is silently reset to `None` (open to all callers) on any upgrade where the allowlist is not explicitly re-supplied in the upgrade arguments.

---

### Finding Description

In `rs/migration_canister/src/canister_state.rs`, the allowlist is declared as a plain non-stable thread-local:

```rust
static ALLOWLIST: RefCell<Option<Vec<Principal>>> = const { RefCell::new(None) };
``` [1](#0-0) 

The `caller_allowed` check treats `None` as "allow all":

```rust
pub fn caller_allowed(id: &Principal) -> bool {
    ALLOWLIST.with_borrow(|allowlist| match allowlist {
        Some(allowlist) => allowlist.contains(id),
        None => true,
    })
}
``` [2](#0-1) 

The allowlist is set only in `init` and `post_upgrade`:

```rust
#[init]
fn init(args: MigrationCanisterInitArgs) {
    start_timers();
    set_allowlist(args.allowlist);
}

#[post_upgrade]
fn post_upgrade(args: MigrationCanisterInitArgs) {
    start_timers();
    set_allowlist(args.allowlist);
}
``` [3](#0-2) 

The public Candid interface exposes no endpoint to add or remove individual principals at runtime:

```
service : (MigrationCanisterInitArgs) -> {
  disable_api : () -> (MigrationCanisterResult);
  enable_api  : () -> (MigrationCanisterResult);
  migrate_canister : (MigrateCanisterArgs) -> (ValidationResult);
  migration_status : (MigrateCanisterArgs) -> (opt MigrationStatus) query;
}
``` [4](#0-3) 

Contrast this with the `DISABLED` flag, which IS stored in stable memory and can be toggled at runtime via `enable_api`/`disable_api`:

```rust
static DISABLED: RefCell<Cell<bool, Memory>> = ...
``` [5](#0-4) 

The `ALLOWLIST` has no equivalent stable-memory backing and no equivalent runtime toggle.

---

### Impact Explanation

**Inability to selectively revoke access**: If a principal on the allowlist needs to be removed (e.g., a compromised key, a misbehaving caller), the only options are:
1. A full canister upgrade with a new allowlist — requires controller access, preparation time, and governance coordination. During this window, the principal retains access.
2. Calling `disable_api` — a blunt instrument that stops **all** migrations for all principals, not just the offending one.

**Silent allowlist erasure on upgrade**: Because `ALLOWLIST` is not in stable memory, any upgrade that passes `allowlist: None` (or omits the field, defaulting to `None`) silently opens `migrate_canister` to **all callers**, bypassing the soft-rollout gate entirely. There is also no query endpoint to inspect the current allowlist state, making this failure mode invisible. [6](#0-5) 

---

### Likelihood Explanation

**Medium-low**. The allowlist is explicitly described as a "soft rollout" mechanism. The `migrate_canister` function also requires the caller to be a controller of both the migrated and replaced canisters, which limits the blast radius of an allowlisted-but-malicious principal. However, the silent erasure on upgrade is a realistic operational mistake, and the lack of a selective removal endpoint is a structural gap that becomes relevant whenever a principal's access needs to be revoked urgently. [7](#0-6) 

---

### Recommendation

1. **Store the allowlist in stable memory** (using `ic_stable_structures::Cell` or a `BTreeSet` backed by stable memory), consistent with how `DISABLED` is stored, so it survives upgrades without needing to be re-supplied in upgrade arguments.

2. **Expose a runtime `update_allowlist` endpoint** (controller-gated, analogous to `enable_api`/`disable_api`) that allows adding and removing individual principals without a full canister upgrade.

3. **Add a query endpoint** to inspect the current allowlist, so operators can verify the state without performing an upgrade.

---

### Proof of Concept

**Scenario — silent erasure**:
1. Canister is deployed with `allowlist: Some(vec![principal_A])`.
2. A controller upgrades the canister, accidentally passing `allowlist: None` in the upgrade args (or using a default-constructed `MigrationCanisterInitArgs`).
3. `set_allowlist(None)` is called; `ALLOWLIST` becomes `None`.
4. `caller_allowed` now returns `true` for **any** caller, bypassing the soft-rollout gate.
5. Any unprivileged ingress sender can now call `migrate_canister` (subject only to the controller-of-canisters check).

**Scenario — no selective revocation**:
1. `principal_A` is on the allowlist and their key is compromised.
2. The controller cannot remove `principal_A` without a full upgrade.
3. During upgrade preparation, `principal_A` continues to call `migrate_canister` for any canisters they control.
4. The only immediate mitigation is `disable_api`, which also blocks all legitimate callers. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/migration_canister/src/canister_state.rs (L17-18)
```rust
thread_local! {
    static ALLOWLIST: RefCell<Option<Vec<Principal>>> = const { RefCell::new(None) };
```

**File:** rs/migration_canister/src/canister_state.rs (L27-28)
```rust
    static DISABLED: RefCell<Cell<bool, Memory>> =
        RefCell::new(Cell::init(MEMORY_MANAGER.with(|m| m.borrow().get(MemoryId::new(0))), false));
```

**File:** rs/migration_canister/src/canister_state.rs (L60-69)
```rust
pub fn set_allowlist(arg: Option<Vec<Principal>>) {
    ALLOWLIST.set(arg);
}

pub fn caller_allowed(id: &Principal) -> bool {
    ALLOWLIST.with_borrow(|allowlist| match allowlist {
        Some(allowlist) => allowlist.contains(id),
        None => true,
    })
}
```

**File:** rs/migration_canister/src/migration_canister.rs (L31-41)
```rust
#[init]
fn init(args: MigrationCanisterInitArgs) {
    start_timers();
    set_allowlist(args.allowlist);
}

#[post_upgrade]
fn post_upgrade(args: MigrationCanisterInitArgs) {
    start_timers();
    set_allowlist(args.allowlist);
}
```

**File:** rs/migration_canister/src/migration_canister.rs (L73-77)
```rust
    let caller = msg_caller();
    // For soft rollout purposes
    if !caller_allowed(&caller) {
        return Err(Some(ValidationError::MigrationsDisabled(Reserved)));
    }
```

**File:** rs/migration_canister/migration_canister.did (L33-38)
```text
service : (MigrationCanisterInitArgs) -> {
  disable_api : () -> (MigrationCanisterResult);
  enable_api : () -> (MigrationCanisterResult);
  migrate_canister : (MigrateCanisterArgs) -> (ValidationResult);
  migration_status : (MigrateCanisterArgs) -> (opt MigrationStatus) query;
}
```

**File:** rs/migration_canister/src/privileged.rs (L27-38)
```rust
#[update]
fn enable_api() -> Result<(), Option<MigrationCanisterError>> {
    check_caller()?;
    set_disabled_flag(false);
    Ok(())
}

#[update]
fn disable_api() -> Result<(), Option<MigrationCanisterError>> {
    check_caller()?;
    set_disabled_flag(true);
    Ok(())
```
