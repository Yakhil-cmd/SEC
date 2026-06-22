### Title
`disable_api` Emergency Shutdown Bypass: In-Flight Migrations Continue Processing After Protocol Disable - (`rs/migration_canister/src/lib.rs`)

---

### Summary

The migration canister's `disable_api()` emergency shutdown mechanism only blocks new `migrate_canister()` ingress calls but does not halt the timer-driven processing pipeline. All in-flight migration requests already queued in `REQUESTS` continue to be processed — including irreversible operations such as deleting canisters and updating the routing table — even after the protocol is disabled.

---

### Finding Description

The migration canister implements a disable mechanism via a stable `DISABLED` flag. Controllers and the NNS governance canister can call `disable_api()` to set this flag, and `enable_api()` to clear it. [1](#0-0) 

The flag is checked in exactly one place: the `migrate_canister()` update handler, which rejects new requests when disabled. [2](#0-1) 

However, `start_timers()` registers nine independent 1-second interval timers that drive the entire migration state machine: [3](#0-2) 

None of these timer callbacks — `process_accepted`, `process_controllers_changed`, `process_stopped`, `process_renamed`, `process_updated`, `process_routing_table`, `process_migrated_canister_deleted`, `process_all_succeeded`, `process_all_failed` — check `migrations_disabled()` before executing. [4](#0-3) 

The processing pipeline performs irreversible operations:
- `process_accepted`: takes exclusive controller ownership of both canisters
- `process_renamed`: renames the replaced canister via the management canister
- `process_renamed` → `process_updated`: calls `migrate_canister` on the registry to update the routing table
- `process_routing_table`: **permanently deletes** the migrated canister [5](#0-4) 

---

### Impact Explanation

When governance or a controller calls `disable_api()` — for example, upon discovering a bug in the migration logic — any requests already in `REQUESTS` continue to be driven to completion by the timers. This includes:

1. **Irreversible canister deletion**: `delete_canister` is called on the migrated canister's original subnet. Once deleted, the canister and its state are gone permanently.
2. **Routing table corruption**: The registry's routing table is updated to point the migrated canister ID to the replaced canister's subnet. If the disable was triggered because of a routing bug, the corrupted routing entry is still written.
3. **Controller hijacking persists**: The migration canister takes exclusive controller ownership of both canisters in `process_accepted`. If the migration then fails mid-way after disable, the original controllers may not be restored correctly.

The intent of `disable_api` is to halt all migration activity — the integration test explicitly calls it "pausing migrations" and the `MigrationsDisabled` error variant is returned to callers — but the actual effect is only to block the ingress entry point. [6](#0-5) 

---

### Likelihood Explanation

The entry path is reachable by any unprivileged ingress sender who controls both the `migrated_canister_id` and `replaced_canister_id` (i.e., is a controller of their own canisters — a normal user role). The scenario is:

1. User calls `migrate_canister()` with their own canisters. The request passes validation and is inserted into `REQUESTS` as `RequestState::Accepted`.
2. Governance or a controller calls `disable_api()` (e.g., due to a discovered bug).
3. The timers continue firing every second and drive the accepted request through all processing stages, including the irreversible delete and routing table update.

This is a realistic incident-response scenario. The window between request acceptance and the disable call can be seconds to minutes, and the timer fires every second, so the race is easily won by the processing pipeline.

---

### Recommendation

Add a `migrations_disabled()` check at the top of `process_all_by_predicate` (or in each individual `process_*` function) so that the timer-driven pipeline halts when the protocol is disabled:

```rust
pub async fn process_all_by_predicate<F>(
    tag: &str,
    predicate: impl Fn(&RequestState) -> bool,
    processor: impl Fn(RequestState) -> F,
) where
    F: Future<Output = ProcessingResult<RequestState, RequestState>>,
{
    if migrations_disabled() {
        return;
    }
    // ... existing logic
}
```

Similarly, `process_all_failed` and `process_all_succeeded` should also check the flag. This ensures that calling `disable_api()` truly halts all migration activity, not just new ingress requests.

---

### Proof of Concept

1. User calls `migrate_canister({ migrated_canister_id: A, replaced_canister_id: B })` where they control both `A` and `B`. Request is accepted and stored as `RequestState::Accepted` in stable `REQUESTS`.
2. Governance calls `disable_api()`. `DISABLED` flag is set to `true`.
3. The 1-second timer fires. `process_all_by_predicate("accepted", ...)` does **not** check `migrations_disabled()` and calls `process_accepted`, which calls `set_exclusive_controller` on both `A` and `B`, taking ownership away from the original controllers.
4. Subsequent timer ticks drive the request through `ControllersChanged` → `StoppedAndReady` → `RenamedReplacedCanister` → `UpdatedRoutingTable` → `RoutingTableChangeAccepted` → `MigratedCanisterDeleted`, permanently deleting canister `A` and updating the registry routing table — all while `migrations_disabled()` returns `true`. [7](#0-6) [5](#0-4) [3](#0-2)

### Citations

**File:** rs/migration_canister/src/privileged.rs (L34-38)
```rust
#[update]
fn disable_api() -> Result<(), Option<MigrationCanisterError>> {
    check_caller()?;
    set_disabled_flag(true);
    Ok(())
```

**File:** rs/migration_canister/src/migration_canister.rs (L62-65)
```rust
async fn migrate_canister(args: MigrateCanisterArgs) -> Result<(), Option<ValidationError>> {
    if migrations_disabled() {
        return Err(Some(ValidationError::MigrationsDisabled(Reserved)));
    }
```

**File:** rs/migration_canister/src/lib.rs (L47-48)
```rust
pub enum ValidationError {
    MigrationsDisabled(Reserved),
```

**File:** rs/migration_canister/src/lib.rs (L460-523)
```rust
pub fn start_timers() {
    let interval = Duration::from_secs(1);
    set_timer_interval(interval, async || {
        process_all_by_predicate(
            "accepted",
            |r| matches!(r, RequestState::Accepted { .. }),
            process_accepted,
        )
        .await
    });
    set_timer_interval(interval, async || {
        process_all_by_predicate(
            "controllers_changed",
            |r| matches!(r, RequestState::ControllersChanged { .. }),
            process_controllers_changed,
        )
        .await
    });
    set_timer_interval(interval, async || {
        process_all_by_predicate(
            "stopped",
            |r| matches!(r, RequestState::StoppedAndReady { .. }),
            process_stopped,
        )
        .await
    });
    set_timer_interval(interval, async || {
        process_all_by_predicate(
            "renamed_replaced_canister",
            |r| matches!(r, RequestState::RenamedReplacedCanister { .. }),
            process_renamed,
        )
        .await
    });
    set_timer_interval(interval, async || {
        process_all_by_predicate(
            "updated_routing_table",
            |r| matches!(r, RequestState::UpdatedRoutingTable { .. }),
            process_updated,
        )
        .await
    });
    set_timer_interval(interval, async || {
        process_all_by_predicate(
            "routing_table_change_accepted",
            |r| matches!(r, RequestState::RoutingTableChangeAccepted { .. }),
            process_routing_table,
        )
        .await
    });
    set_timer_interval(interval, async || {
        process_all_by_predicate(
            "migrated_canister_deleted",
            |r| matches!(r, RequestState::MigratedCanisterDeleted { .. }),
            process_migrated_canister_deleted,
        )
        .await
    });

    set_timer_interval(interval, async || process_all_succeeded().await);

    // This one has a different type from the generic ones above.
    set_timer_interval(interval, async || process_all_failed().await);
}
```

**File:** rs/migration_canister/src/processing.rs (L35-71)
```rust
pub async fn process_all_by_predicate<F>(
    tag: &str,
    predicate: impl Fn(&RequestState) -> bool,
    processor: impl Fn(RequestState) -> F,
) where
    F: Future<Output = ProcessingResult<RequestState, RequestState>>,
{
    // Ensures this method runs only once at any given time.
    let Ok(_guard) = MethodGuard::new(tag) else {
        return;
    };
    let mut tasks = vec![];
    let requests = list_by(predicate);
    if requests.is_empty() {
        return;
    }
    println!(
        "Entering `{}` with {} pending requests",
        tag,
        requests.len()
    );
    for request in requests.iter() {
        tasks.push(processor(request.clone()));
    }
    let results = join_all(tasks).await;
    let mut success_counter = 0;
    for (req, res) in zip(requests, results) {
        if res.is_success() {
            success_counter += 1;
        }
        res.transition(req);
    }
    println!(
        "Exiting `{}` with {} successful transitions.",
        tag, success_counter
    );
}
```

**File:** rs/migration_canister/src/processing.rs (L75-109)
```rust
pub async fn process_accepted(
    request: RequestState,
) -> ProcessingResult<RequestState, RequestState> {
    let RequestState::Accepted { request } = request else {
        println!("Error: list_by Accepted returned bad variant");
        return ProcessingResult::NoProgress;
    };

    // Set controller of migrated canister
    let res = set_exclusive_controller(request.migrated_canister)
        .await
        .map_success(|_| RequestState::ControllersChanged {
            request: request.clone(),
        })
        .map_failure(|reason| RequestState::Failed {
            request: request.clone(),
            recovery_state: RecoveryState::new(),
            reason,
        });
    if !res.is_success() {
        return res;
    }

    // Set controller of replaced canister
    set_exclusive_controller(request.replaced_canister)
        .await
        .map_success(|_| RequestState::ControllersChanged {
            request: request.clone(),
        })
        .map_failure(|reason| RequestState::Failed {
            request,
            recovery_state: RecoveryState::new(),
            reason,
        })
}
```

**File:** rs/migration_canister/src/processing.rs (L287-307)
```rust
pub async fn process_routing_table(
    request: RequestState,
) -> ProcessingResult<RequestState, RequestState> {
    let RequestState::RoutingTableChangeAccepted {
        request,
        stopped_since,
    } = request
    else {
        println!("Error: list_by RoutingTableChangeAccepted returned bad variant");
        return ProcessingResult::NoProgress;
    };
    let ProcessingResult::Success(()) =
        delete_canister(request.migrated_canister, request.migrated_canister_subnet).await
    else {
        return ProcessingResult::NoProgress;
    };
    ProcessingResult::Success(RequestState::MigratedCanisterDeleted {
        request,
        stopped_since,
    })
}
```
