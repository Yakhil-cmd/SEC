### Title
In-Flight Migrations Continue Processing After `disable_api()` Pause — (`rs/migration_canister/src/lib.rs`, `rs/migration_canister/src/processing.rs`)

### Summary

The migration canister exposes `disable_api()` so that governance/controllers can pause all canister migration activity. However, the `migrations_disabled()` flag is only checked in the `migrate_canister()` ingress handler. The timer-driven processing pipeline — which advances already-queued requests through every subsequent state — never consults this flag. Any migration request accepted before `disable_api()` is called will run to completion, including irreversible steps such as deleting the migrated canister and updating the routing table, even while the API is officially paused.

### Finding Description

**Pause entry point — only blocks new submissions:**

`migrate_canister()` checks `migrations_disabled()` at the very top and returns `Err(ValidationError::MigrationsDisabled)` if the flag is set. [1](#0-0) 

**Timer pipeline — never checks the flag:**

`start_timers()` registers nine `set_timer_interval` callbacks (one per `RequestState` variant). Each callback calls a `process_*` function. None of these functions call `migrations_disabled()`. [2](#0-1) 

The individual processing functions (`process_accepted`, `process_controllers_changed`, `process_stopped`, `process_renamed`, `process_updated`, `process_routing_table`, `process_migrated_canister_deleted`, `process_all_failed`, `process_all_succeeded`) contain no reference to `migrations_disabled()`. [3](#0-2) [4](#0-3) 

**The `DISABLED` flag is only read in one place:** [5](#0-4) 

**Exploit path:**

1. Attacker (a canister controller who is also on the allowlist) calls `migrate_canister()` while the API is enabled. The request passes all validation and is inserted into `REQUESTS` as `RequestState::Accepted`.
2. Governance submits a `PauseCanisterMigrations` NNS proposal, which calls `disable_api()` on the migration canister.
3. The `DISABLED` flag is now `true`. New calls to `migrate_canister()` are rejected with `MigrationsDisabled`.
4. The timer fires every second and calls `process_accepted` → `process_controllers_changed` → `process_stopped` → `process_renamed` → `process_updated` → `process_routing_table` → `process_migrated_canister_deleted` → `process_all_succeeded`. None of these check `migrations_disabled()`.
5. The migration completes in full: the migrated canister is deleted, the routing table is updated in the registry, and controllers are restored — all while the API is officially paused.

The NNS governance test helper confirms the intent of the pause: [6](#0-5) 

The system test confirms that `migrate_canister()` is blocked after pausing, but does not verify that in-flight requests are halted: [7](#0-6) 

### Impact Explanation

The migration process includes irreversible operations: `delete_canister` on the migrated canister's original subnet and `migrate_canisters` on the registry canister to update the routing table. [4](#0-3) [8](#0-7) 

If governance pauses migrations because a bug is discovered in the migration pipeline itself, in-flight requests will still execute those irreversible steps. This can result in:
- A canister being permanently deleted from its original subnet while the routing table update is incomplete or incorrect.
- The migrated canister's controllers being changed to the migration canister exclusively during the window, with no guarantee of recovery if the process is interrupted mid-flight.
- Governance losing the ability to halt a migration that is actively causing harm.

### Likelihood Explanation

The `disable_api()` / `PauseCanisterMigrations` mechanism exists precisely for emergency use. The scenario where governance needs to stop all migration activity immediately (e.g., a bug is found in `process_renamed` or `process_routing_table`) is the primary motivation for the pause feature. A user who submitted a migration request in the seconds before the pause — or who deliberately races the pause — will have their migration continue through all irreversible steps. The attacker precondition (being a controller of both canisters and on the allowlist) is the normal operating condition for any legitimate migration user.

### Recommendation

Add a `migrations_disabled()` guard to the early processing stages. At minimum, `process_accepted` should check the flag and return `ProcessingResult::NoProgress` (causing the request to stall without advancing) when the API is disabled:

```rust
pub async fn process_accepted(
    request: RequestState,
) -> ProcessingResult<RequestState, RequestState> {
+   if migrations_disabled() {
+       return ProcessingResult::NoProgress;
+   }
    let RequestState::Accepted { request } = request else { ... };
    ...
}
```

Blocking at `process_accepted` is sufficient to prevent any irreversible action, since all destructive steps occur in later states. Requests already past `Accepted` (e.g., `ControllersChanged`) may also need a guard depending on the desired semantics of the pause.

### Proof of Concept

```
// 1. User submits migrate_canister() — succeeds, request enters REQUESTS as Accepted.
// 2. Governance calls disable_api() — DISABLED = true.
// 3. Timer fires: process_accepted() runs, no migrations_disabled() check,
//    transitions request to ControllersChanged.
// 4. Timer fires: process_controllers_changed() runs, no check,
//    transitions to StoppedAndReady.
// ... (continues through all states)
// 5. process_routing_table() calls delete_canister() — migrated canister is deleted.
// 6. process_all_succeeded() removes request from REQUESTS, inserts Succeeded event.
// Migration is complete despite DISABLED = true.
```

The existing unit test `validation_fails_disabled` only verifies that `migrate_canister()` is rejected when disabled; it does not test that the timer pipeline is halted. [9](#0-8)

### Citations

**File:** rs/migration_canister/src/migration_canister.rs (L62-65)
```rust
async fn migrate_canister(args: MigrateCanisterArgs) -> Result<(), Option<ValidationError>> {
    if migrations_disabled() {
        return Err(Some(ValidationError::MigrationsDisabled(Reserved)));
    }
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

**File:** rs/migration_canister/src/processing.rs (L73-110)
```rust
/// Accepts an `Accepted` request, returns `ControllersChanged` on success.
/// This function is an exception in that it tries to make _two_ effectful calls.
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

**File:** rs/migration_canister/src/processing.rs (L231-251)
```rust
pub async fn process_renamed(
    request: RequestState,
) -> ProcessingResult<RequestState, RequestState> {
    let RequestState::RenamedReplacedCanister {
        request,
        stopped_since,
    } = request
    else {
        println!("Error: list_by RenamedReplacedCanister returned bad variant");
        return ProcessingResult::NoProgress;
    };

    migrate_canister(request.migrated_canister, request.replaced_canister_subnet)
        .await
        .map_success(|registry_version| RequestState::UpdatedRoutingTable {
            request,
            stopped_since,
            registry_version,
        })
        .or_retry()
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

**File:** rs/migration_canister/src/canister_state.rs (L56-58)
```rust
pub fn migrations_disabled() -> bool {
    DISABLED.with_borrow(|x| *x.get())
}
```

**File:** rs/nns/test_utils/src/governance.rs (L574-587)
```rust
pub async fn pause_canister_migrations(governance: &Canister<'_>) {
    let proposal: Vec<u8> = Vec::new();

    submit_external_update_proposal(
        governance,
        Sender::from_keypair(&TEST_NEURON_1_OWNER_KEYPAIR),
        NeuronId(TEST_NEURON_1_ID),
        NnsFunction::PauseCanisterMigrations,
        proposal,
        "Pause Canister Migrations".to_string(),
        "".to_string(),
    )
    .await;
}
```

**File:** rs/tests/execution/canister_migration_test.rs (L377-407)
```rust
    info!(logger, "Pausing migrations");

    pause_canister_migrations(&governance_canister).await;

    let args = Encode!(&MigrateCanisterArgs {
        migrated_canister_id: migrated_canister.canister_id(),
        replaced_canister_id: replaced_canister.canister_id(),
    })
    .unwrap();
    let args2 = Encode!(&MigrateCanisterArgs {
        migrated_canister_id: migrated_canister2.canister_id(),
        replaced_canister_id: replaced_canister2.canister_id(),
    })
    .unwrap();

    info!(logger, "Calling migrate_canister on paused canister");

    let result = nns_agent
        .update(&migration_canister_id, "migrate_canister")
        .with_arg(args.clone())
        .call_and_wait()
        .await
        .expect("Failed to call migrate_canister.");

    let decoded_result = Decode!(&result, Result<(), Option<ValidationError>>)
        .expect("Failed to decode reponse from migrate_canister.");

    assert_eq!(
        decoded_result,
        Err(Some(ValidationError::MigrationsDisabled(Reserved)))
    );
```

**File:** rs/migration_canister/tests/tests.rs (L1243-1278)
```rust
#[tokio::test]
async fn validation_fails_disabled() {
    let Setup {
        pic,
        migrated_canisters,
        replaced_canisters,
        migrated_canister_controllers,
        system_controller,
        ..
    } = setup(Settings::default()).await;
    let sender = migrated_canister_controllers[0];
    let migrated_canister = migrated_canisters[0];
    let replaced_canister = replaced_canisters[0];
    // disable canister API
    pic.update_call(
        MIGRATION_CANISTER_ID.into(),
        system_controller,
        "disable_api",
        Encode!().unwrap(),
    )
    .await
    .unwrap();

    assert!(matches!(
        migrate_canister(
            &pic,
            sender,
            &MigrateCanisterArgs {
                migrated_canister_id: migrated_canister,
                replaced_canister_id: replaced_canister
            }
        )
        .await,
        Err(ValidationError::MigrationsDisabled(Reserved))
    ));
}
```
