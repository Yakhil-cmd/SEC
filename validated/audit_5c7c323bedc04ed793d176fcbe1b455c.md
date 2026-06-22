### Title
Non-Stable `ALLOWLIST` Storage Silently Resets Access Control on Upgrade - (File: rs/migration_canister/src/canister_state.rs)

### Summary

The Migration Canister stores its `ALLOWLIST` (the access-control gate for `migrate_canister`) in heap memory (`thread_local! RefCell`), while the `DISABLED` flag — the other security-critical switch — is stored in `ic_stable_structures` stable memory. On every canister upgrade, heap memory is wiped. If the upgrade argument supplies `allowlist: None`, the allowlist is silently reset to `None`, which the `caller_allowed` function interprets as "allow everyone." The `DISABLED` flag, by contrast, survives upgrades automatically because it lives in stable memory. This is the direct IC analog of using a non-upgradeable library alongside an upgradeable one: two security controls that should behave symmetrically across upgrades do not.

### Finding Description

In `rs/migration_canister/src/canister_state.rs`, the two security controls are declared as follows:

```rust
// Heap memory — wiped on upgrade
static ALLOWLIST: RefCell<Option<Vec<Principal>>> = const { RefCell::new(None) };

// Stable memory — survives upgrade automatically
static DISABLED: RefCell<Cell<bool, Memory>> =
    RefCell::new(Cell::init(MEMORY_MANAGER.with(|m| m.borrow().get(MemoryId::new(0))), false));
``` [1](#0-0) 

The `caller_allowed` function treats `None` as "no restriction":

```rust
pub fn caller_allowed(id: &Principal) -> bool {
    ALLOWLIST.with_borrow(|allowlist| match allowlist {
        Some(allowlist) => allowlist.contains(id),
        None => true,   // open access
    })
}
``` [2](#0-1) 

The `post_upgrade` hook re-applies the allowlist from the upgrade argument:

```rust
#[post_upgrade]
fn post_upgrade(args: MigrationCanisterInitArgs) {
    start_timers();
    set_allowlist(args.allowlist);
}
``` [3](#0-2) 

`MigrationCanisterInitArgs.allowlist` is typed `Option<Vec<Principal>>`, so passing `allowlist: None` is a valid, well-formed upgrade argument. When that happens, `set_allowlist(None)` is called, `ALLOWLIST` becomes `None`, and every subsequent call to `caller_allowed` returns `true`. The `DISABLED` flag, stored in stable memory, is unaffected and retains whatever value it had before the upgrade. [4](#0-3) 

The `migrate_canister` update method enforces the allowlist check:

```rust
if !caller_allowed(&caller) {
    return Err(Some(ValidationError::MigrationsDisabled(Reserved)));
}
``` [5](#0-4) 

Once the allowlist is `None`, this guard is bypassed for every caller.

### Impact Explanation

`migrate_canister` is a high-privilege operation: it temporarily seizes sole controller rights over both the migrated and replaced canisters, stops them, rewrites the subnet routing table via a registry call, deletes the original canister, and restores controllers. [6](#0-5) 

With the allowlist bypassed, any unprivileged ingress sender who is a controller of two canisters on different subnets can:

1. Trigger migrations that were not intended to be available during the soft-rollout phase.
2. Exhaust the global rate limit of 50 migrations per 24-hour window (`RATE_LIMIT = 50`), denying service to legitimately authorized users. [7](#0-6) 
3. Cause the migration canister to temporarily hold sole controller rights over the caller's canisters, creating a window for disruption if the migration fails mid-flight and the recovery path is exercised.

The `DISABLED` flag, which is the coarser "kill switch," persists correctly across upgrades, so operators may incorrectly believe both security controls are equally durable.

### Likelihood Explanation

The `post_upgrade` argument type makes `allowlist: None` a natural default when scripting or automating upgrades. The code comment "For soft rollout purposes" signals that the allowlist is expected to be actively managed, increasing the chance that an operator omits it during a routine upgrade. No privileged key compromise or subnet-majority attack is required — only a valid upgrade call with a `None` allowlist argument, which any canister controller (including the NNS governance canister) can issue.

### Recommendation

Store `ALLOWLIST` in stable memory using `ic_stable_structures::Cell` or `BTreeMap`, exactly as `DISABLED` is stored, so it survives upgrades automatically without requiring re-supply. If the re-supply pattern is intentional, the `post_upgrade` function should explicitly reject an `allowlist: None` argument when a non-`None` allowlist was previously configured, preventing silent open-access resets.

### Proof of Concept

1. Deploy the migration canister with `allowlist: Some(vec![authorized_principal])`.
2. Confirm that calling `migrate_canister` from an unauthorized principal returns `MigrationsDisabled`.
3. Upgrade the canister with `MigrationCanisterInitArgs { allowlist: None }`.
4. Call `migrate_canister` from any principal that controls two canisters on different subnets.
5. Observe the call succeeds — the allowlist check passes because `caller_allowed` returns `true` for `None`.
6. Confirm `migrations_disabled()` still returns `false` (the `DISABLED` flag was unaffected by the upgrade), demonstrating the asymmetry between the two controls.

### Citations

**File:** rs/migration_canister/src/canister_state.rs (L17-28)
```rust
thread_local! {
    static ALLOWLIST: RefCell<Option<Vec<Principal>>> = const { RefCell::new(None) };

    static LOCKS: RefCell<BTreeSet<Lock>> = const {RefCell::new(BTreeSet::new()) };

    static ONGOING_VALIDATIONS: RefCell<u64> = const { RefCell::new(0)};

    static MEMORY_MANAGER: RefCell<MemoryManager<DefaultMemoryImpl>> =
        RefCell::new(MemoryManager::init(DefaultMemoryImpl::default()));

    static DISABLED: RefCell<Cell<bool, Memory>> =
        RefCell::new(Cell::init(MEMORY_MANAGER.with(|m| m.borrow().get(MemoryId::new(0))), false));
```

**File:** rs/migration_canister/src/canister_state.rs (L64-69)
```rust
pub fn caller_allowed(id: &Principal) -> bool {
    ALLOWLIST.with_borrow(|allowlist| match allowlist {
        Some(allowlist) => allowlist.contains(id),
        None => true,
    })
}
```

**File:** rs/migration_canister/src/migration_canister.rs (L26-35)
```rust
#[derive(CandidType, Deserialize)]
pub(crate) struct MigrationCanisterInitArgs {
    allowlist: Option<Vec<Principal>>,
}

#[init]
fn init(args: MigrationCanisterInitArgs) {
    start_timers();
    set_allowlist(args.allowlist);
}
```

**File:** rs/migration_canister/src/migration_canister.rs (L37-41)
```rust
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

**File:** rs/migration_canister/src/lib.rs (L40-40)
```rust
const RATE_LIMIT: u64 = 50;
```

**File:** rs/migration_canister/src/lib.rs (L211-308)
```rust
pub enum RequestState {
    /// Request was validated successfully.
    /// * Called registry `get_subnet_for_canister` to determine:
    ///     * Existence of migrated and replaced canisters.
    ///     * Subnet of migrated and replaced canisters.
    /// * Called mgmt `canister_status` to determine:
    ///     * We are controller of migrated and replaced canisters.
    ///     * The original controllers of migrated and replaced canisters.
    ///     * If the replaced canister has sufficient cycles above the freezing threshold.
    #[strum(to_string = "RequestState::Accepted {{ request: {request} }}")]
    Accepted { request: Request },

    /// Called mgmt `update_settings` to make us the only controller.
    ///
    /// Certain checks are not informative before this state because the original controller
    /// could still interfere until this state.
    #[strum(to_string = "RequestState::ControllersChanged {{ request: {request} }}")]
    ControllersChanged { request: Request },

    /// * Called mgmt `canister_status` to determine:
    ///     * Migrated and replaced canisters are stopped.
    ///     * Migrated canister is ready for migration.
    ///     * Replaced canister has no snapshots.
    ///     * Replaced canister has sufficient cycles above the freezing threshold.
    ///     * Migrated canister version is not absurdly high.
    /// * Called mgmt `canister_info` to determine the history length of migrated canister.
    ///
    /// Record the canister version and history length of migrated canister and the current time.
    #[strum(
        to_string = "RequestState::StoppedAndReady {{ request: {request}, stopped_since: {stopped_since}, canister_version: {canister_version}, canister_history_total_num: {canister_history_total_num} }}"
    )]
    StoppedAndReady {
        request: Request,
        stopped_since: u64,
        canister_version: u64,
        canister_history_total_num: u64,
    },

    /// Called mgmt `rename_canister`. Subsequent mgmt calls have to use the explicit subnet ID, not `aaaaa-aa`.
    #[strum(
        to_string = "RequestState::RenamedReplacedCanister {{ request: {request}, stopped_since: {stopped_since} }}"
    )]
    RenamedReplacedCanister {
        request: Request,
        stopped_since: u64,
    },

    /// Called registry `migrate_canisters`.
    ///
    /// Record the new registry version.
    #[strum(
        to_string = "RequestState::UpdatedRoutingTable {{ request: {request}, stopped_since: {stopped_since}, registry_version: {registry_version} }}"
    )]
    UpdatedRoutingTable {
        request: Request,
        stopped_since: u64,
        registry_version: u64,
    },

    /// Both subnets have learned about the new routing information.
    /// Called `subnet_info` on both subnets to determine their `registry_version`.
    #[strum(
        to_string = "RequestState::RoutingTableChangeAccepted {{ request: {request}, stopped_since: {stopped_since} }}"
    )]
    RoutingTableChangeAccepted {
        request: Request,
        stopped_since: u64,
    },

    /// Called mgmt `delete_canister`.
    #[strum(
        to_string = "RequestState::MigratedCanisterDeleted {{ request: {request}, stopped_since: {stopped_since} }}"
    )]
    MigratedCanisterDeleted {
        request: Request,
        stopped_since: u64,
    },

    /// Six minutes have passed since `stopped_since` such that any messages to the
    /// migrated canister subnet have expired by now.
    /// Restored the controllers of the replaced canister (now addressed with migrated canister's id).
    ///
    /// This state transitions to a success event without any additional work.
    ///
    /// Called `update_settings` to restore controllers.
    #[strum(to_string = "RequestState::RestoredControllers {{ request: {request} }}")]
    RestoredControllers { request: Request },

    /// Some transition has failed fatally.
    /// We stay in this state until the controllers have been restored and then
    /// transition to a `Failed` state in the `HISTORY`.
    #[strum(to_string = "RequestState::Failed {{ request: {request}, reason: {reason} }}")]
    Failed {
        request: Request,
        recovery_state: RecoveryState,
        reason: String,
    },
}
```
