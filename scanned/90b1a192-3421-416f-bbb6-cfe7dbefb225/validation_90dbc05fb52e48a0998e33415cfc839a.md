### Title
Uninitialized `ALLOWLIST` in Migration Canister Causes `caller_allowed()` to Always Return `true` - (`rs/migration_canister/src/canister_state.rs`)

### Summary

The `ALLOWLIST` state variable in the migration canister defaults to `None`. The `caller_allowed()` function treats `None` as "allow all," meaning the soft-rollout allowlist check in `migrate_canister()` is permanently bypassed in the default production deployment. Any ingress caller who is also a controller of the migrated canister can invoke `migrate_canister()` without restriction, defeating the intended phased-rollout access control.

---

### Finding Description

In `rs/migration_canister/src/canister_state.rs`, the `ALLOWLIST` thread-local is initialized to `None`: [1](#0-0) 

The `caller_allowed()` function explicitly returns `true` when `ALLOWLIST` is `None`: [2](#0-1) 

In `migrate_canister()`, this check is labeled "For soft rollout purposes" and is supposed to gate access to the migration feature: [3](#0-2) 

The `init` and `post_upgrade` hooks call `set_allowlist(args.allowlist)`, where `args.allowlist` is `Option<Vec<Principal>>`: [4](#0-3) 

The production-like deployment in `rs/pocket_ic_server/src/pocket_ic.rs` installs the migration canister with `MigrationCanisterInitArgs::default()`, which sets `allowlist: None`: [5](#0-4) 

The NNS test utility `install_migration_canister` does the same: [6](#0-5) 

When `allowlist: None` is passed, `set_allowlist(None)` stores `None` in `ALLOWLIST`, and `caller_allowed()` unconditionally returns `true` for every caller. The allowlist restriction is structurally present but never enforced in any default deployment path.

---

### Impact Explanation

The migration canister orchestrates cross-subnet canister migrations — a sensitive operation that modifies the IC's routing table and canister placement. The allowlist was designed to gate this capability to a controlled set of principals during a phased rollout. Because `ALLOWLIST` defaults to `None` and the production deployment never sets it to a non-`None` value, any caller who satisfies the remaining `validate_request()` checks (i.e., is a controller of the migrated canister) can invoke `migrate_canister()` without restriction. The intended soft-rollout access control is completely ineffective. [2](#0-1) 

---

### Likelihood Explanation

The likelihood is high. The default deployment path (`MigrationCanisterInitArgs::default()`) always produces `allowlist: None`. There is no mechanism in the canister's privileged API to set the allowlist post-deployment — `set_allowlist` is only called from `init` and `post_upgrade`. Any canister controller who wants to use the migration feature before the soft rollout is complete can do so freely. [7](#0-6) 

---

### Recommendation

1. Change the default behavior of `caller_allowed()` so that `None` means **deny all** (closed by default), not allow all. Alternatively, rename the sentinel to make the permissive intent explicit and require an explicit `Some(vec![])` or a dedicated `AllowAll` variant.
2. Add a privileged endpoint (callable only by controllers) to update the allowlist post-deployment, so the allowlist can be tightened without a canister upgrade.
3. Ensure the production deployment sets `allowlist` to a non-`None` value that reflects the intended restricted set of principals. [8](#0-7) 

---

### Proof of Concept

1. Deploy the migration canister using the default init args (`allowlist: None`), as done in `deploy_canister_migration` in `pocket_ic.rs`.
2. As any principal `P` who is a controller of a stopped canister `C` on subnet `S1` and a stopped canister `D` on subnet `S2`, call:
   ```
   migrate_canister({ migrated_canister_id = C; replaced_canister_id = D })
   ```
3. `caller_allowed(&P)` returns `true` because `ALLOWLIST` is `None`.
4. The call proceeds to `validate_request()`, which checks controller relationships — not allowlist membership.
5. The migration is accepted and queued, bypassing the intended soft-rollout restriction entirely. [9](#0-8) [1](#0-0)

### Citations

**File:** rs/migration_canister/src/canister_state.rs (L17-18)
```rust
thread_local! {
    static ALLOWLIST: RefCell<Option<Vec<Principal>>> = const { RefCell::new(None) };
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

**File:** rs/migration_canister/src/migration_canister.rs (L61-93)
```rust
#[update]
async fn migrate_canister(args: MigrateCanisterArgs) -> Result<(), Option<ValidationError>> {
    if migrations_disabled() {
        return Err(Some(ValidationError::MigrationsDisabled(Reserved)));
    }
    // Prevent too many interleaved validations.
    let Ok(_guard) = ValidationGuard::new() else {
        return Err(Some(ValidationError::RateLimited(Reserved)));
    };
    if rate_limited() {
        return Err(Some(ValidationError::RateLimited(Reserved)));
    }
    let caller = msg_caller();
    // For soft rollout purposes
    if !caller_allowed(&caller) {
        return Err(Some(ValidationError::MigrationsDisabled(Reserved)));
    }
    match validate_request(args.migrated_canister_id, args.replaced_canister_id, caller).await {
        Err(e) => {
            println!("Failed to validate request {}: {}", args, e);
            return Err(Some(e));
        }
        Ok((request, _guards)) => {
            // Need to check the rate limit again
            if rate_limited() {
                return Err(Some(ValidationError::RateLimited(Reserved)));
            }
            println!("Accepted request {}", request);
            insert_request(RequestState::Accepted { request });
        }
    }
    Ok(())
}
```

**File:** rs/pocket_ic_server/src/pocket_ic.rs (L2625-2636)
```rust
            #[derive(Default, CandidType, Deserialize)]
            struct MigrationCanisterInitArgs {
                allowlist: Option<Vec<Principal>>,
            }
            nns_subnet
                .state_machine
                .install_wasm_in_mode(
                    canister_id,
                    CanisterInstallMode::Install,
                    MIGRATION_CANISTER_WASM.to_vec(),
                    Encode!(&MigrationCanisterInitArgs::default()).unwrap(),
                )
```

**File:** rs/nns/test_utils/src/itest_helpers.rs (L826-837)
```rust
pub async fn install_migration_canister(canister: &mut Canister<'_>) {
    #[derive(CandidType, Deserialize, Default)]
    struct MigrationCanisterInitArgs {
        allowlist: Option<Vec<Principal>>,
    }
    install_rust_canister(
        canister,
        "migration-canister",
        &[],
        Some(Encode!(&MigrationCanisterInitArgs::default()).unwrap()),
    )
    .await;
```
