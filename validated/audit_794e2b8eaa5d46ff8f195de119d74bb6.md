### Title
Heap-Only `ALLOWLIST` Lost on Upgrade Bypasses Access Control — (`rs/migration_canister/src/canister_state.rs`)

---

### Summary

The `migration_canister` stores its access-control allowlist (`ALLOWLIST`) in a heap-only `thread_local!` variable that is never persisted to stable memory. After any canister upgrade, this variable resets to `None`. The `caller_allowed()` function treats `None` as "allow everyone," meaning every unprivileged ingress or canister caller gains unrestricted access to the migration canister's public API immediately after an upgrade.

---

### Finding Description

In `rs/migration_canister/src/canister_state.rs`, three security-critical state variables are declared as heap-only `thread_local!` cells:

```rust
thread_local! {
    static ALLOWLIST: RefCell<Option<Vec<Principal>>> = const { RefCell::new(None) };
    static LOCKS: RefCell<BTreeSet<Lock>> = const { RefCell::new(BTreeSet::new()) };
    static ONGOING_VALIDATIONS: RefCell<u64> = const { RefCell::new(0) };
    ...
}
``` [1](#0-0) 

In contrast, the data structures (`DISABLED`, `REQUESTS`, `LIMITER`, `HISTORY`, `LAST_EVENT`) are correctly backed by stable memory via `MemoryManager`. [2](#0-1) 

The `caller_allowed()` function uses `ALLOWLIST` to gate access:

```rust
pub fn caller_allowed(id: &Principal) -> bool {
    ALLOWLIST.with_borrow(|allowlist| match allowlist {
        Some(allowlist) => allowlist.contains(id),
        None => true,   // <-- open to everyone when None
    })
}
``` [3](#0-2) 

Because `ALLOWLIST` is heap-only, it is **not** serialized during `pre_upgrade` and is **not** restored during `post_upgrade`. After every upgrade the variable resets to its initial value of `None`, and `caller_allowed()` unconditionally returns `true` for every caller until a controller explicitly re-sets the allowlist via `set_allowlist`.

The same upgrade-reset problem affects `LOCKS` and `ONGOING_VALIDATIONS`:

- `LOCKS` resetting to empty clears all in-flight async reentrancy guards, potentially allowing concurrent execution of operations that were designed to be mutually exclusive.
- `ONGOING_VALIDATIONS` resetting to `0` bypasses the rate-limit on concurrent validation calls (`MAX_ONGOING_VALIDATIONS`). [1](#0-0) 

---

### Impact Explanation

After any upgrade of the migration canister, every unprivileged canister or ingress caller passes the `caller_allowed()` check. The migration canister orchestrates canister-to-canister migrations (controller transfers, code swaps), so an unauthorized caller can:

1. Submit arbitrary migration requests that the canister will process.
2. Trigger cross-subnet management-canister calls (`install_code`, controller changes) on behalf of the migration canister.
3. Exhaust the rate-limit window by flooding validation requests (since `ONGOING_VALIDATIONS` is also reset).

The window of exposure lasts from the moment the upgrade completes until a controller calls `set_allowlist` again — a window that may be indefinite if operators are unaware of the reset.

---

### Likelihood Explanation

Canister upgrades are routine operational events on the IC. Any time the migration canister is upgraded (e.g., to fix a bug or add a feature), the allowlist silently resets. An attacker who monitors the canister's upgrade history (observable via `read_state` / replica logs) can time a call to arrive immediately after an upgrade. No privileged access, key material, or threshold corruption is required — only the ability to send an ingress message or inter-canister call.

---

### Recommendation

Persist `ALLOWLIST` in stable memory using the same `MemoryManager` pattern already used for `DISABLED`, `REQUESTS`, etc. Assign it a new `MemoryId` and store it as a `StableCell<Option<Vec<Principal>>, Memory>`. Similarly, document that `LOCKS` and `ONGOING_VALIDATIONS` are intentionally ephemeral and add a post-upgrade guard that rejects new requests until the allowlist is explicitly re-initialized, or restore them from stable memory as well.

```rust
const ALLOWLIST_MEMORY_ID: MemoryId = MemoryId::new(5);

thread_local! {
    static ALLOWLIST: RefCell<StableCell<Option<Vec<Principal>>, Memory>> =
        RefCell::new(StableCell::init(
            MEMORY_MANAGER.with(|m| m.borrow().get(ALLOWLIST_MEMORY_ID)),
            None,
        ).expect("failed to initialize allowlist cell"));
}
```

---

### Proof of Concept

1. Controller sets the allowlist to `[principal_A]` via `set_allowlist(Some(vec![principal_A]))`.
2. `caller_allowed(&principal_B)` returns `false` — correct.
3. Controller upgrades the migration canister (routine operation).
4. `ALLOWLIST` resets to `None` (heap state is discarded by the IC runtime).
5. Attacker (holding `principal_B`) immediately calls any public migration endpoint.
6. `caller_allowed(&principal_B)` evaluates `None => true` and returns `true`.
7. The migration canister processes the attacker's request with full authority. [4](#0-3)

### Citations

**File:** rs/migration_canister/src/canister_state.rs (L17-22)
```rust
thread_local! {
    static ALLOWLIST: RefCell<Option<Vec<Principal>>> = const { RefCell::new(None) };

    static LOCKS: RefCell<BTreeSet<Lock>> = const {RefCell::new(BTreeSet::new()) };

    static ONGOING_VALIDATIONS: RefCell<u64> = const { RefCell::new(0)};
```

**File:** rs/migration_canister/src/canister_state.rs (L27-48)
```rust
    static DISABLED: RefCell<Cell<bool, Memory>> =
        RefCell::new(Cell::init(MEMORY_MANAGER.with(|m| m.borrow().get(MemoryId::new(0))), false));

    static REQUESTS: RefCell<BTreeMap<RequestState, (), Memory>> =
        RefCell::new(BTreeMap::init(MEMORY_MANAGER.with(|m| m.borrow().get(MemoryId::new(1)))));

    /// Stores timestamps of all successful events in `HISTORY`
    /// that are within the last 24 hours.
    /// It can also store timestamps beyond the last 24 hours
    /// until they are pruned.
    /// The timestamps are represented as a key-value store
    /// with timestamps as keys and their counts as values.
    static LIMITER: RefCell<BTreeMap<u64, u64, Memory>> = RefCell::new(BTreeMap::init(MEMORY_MANAGER.with(|m| m.borrow().get(MemoryId::new(2)))));

    /// Stores all events indexed by their sequence numbers
    /// in the order of creation.
    static HISTORY: RefCell<BTreeMap<u64, Event, Memory>> =
        RefCell::new(BTreeMap::init(MEMORY_MANAGER.with(|m| m.borrow().get(MemoryId::new(3)))));

    /// Caches the index of the last event for a given pair of migrated and replaced canisters.
    static LAST_EVENT: RefCell<BTreeMap<CanisterMigrationArgs, u64, Memory>> =
        RefCell::new(BTreeMap::init(MEMORY_MANAGER.with(|m| m.borrow().get(MemoryId::new(4)))));
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
