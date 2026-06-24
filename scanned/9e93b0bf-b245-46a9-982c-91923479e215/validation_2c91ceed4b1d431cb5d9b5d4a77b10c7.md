### Title
Migration Canister Timer Callbacks Bypass `migrations_disabled` Flag When API Is Disabled - (File: `rs/migration_canister/src/processing.rs`)

### Summary
The migration canister exposes `disable_api` to halt migration activity. The public `migrate_canister` endpoint correctly checks `migrations_disabled()` and rejects new requests. However, all timer-driven processing callbacks that advance already-accepted requests through the migration pipeline never check this flag, so in-flight migrations continue executing irreversible on-chain operations even after an operator disables the API.

### Finding Description
`disable_api` in `rs/migration_canister/src/privileged.rs` sets a stable `DISABLED` flag via `set_disabled_flag(true)`. The public ingress endpoint `migrate_canister` in `rs/migration_canister/src/migration_canister.rs` checks this flag at the very first line:

```rust
if migrations_disabled() {
    return Err(Some(ValidationError::MigrationsDisabled(Reserved)));
}
``` [1](#0-0) 

However, every timer-scheduled processing function in `rs/migration_canister/src/processing.rs` — `process_accepted`, `process_controllers_changed`, `process_stopped`, `process_renamed`, `process_updated`, `process_routing_table`, `process_migrated_canister_deleted`, and `process_all_failed` — contains no call to `migrations_disabled()` and no early-exit guard on the disabled flag. [2](#0-1) [3](#0-2) [4](#0-3) 

These callbacks perform irreversible operations: exclusively seizing controller of both the migrated and replaced canisters, deleting the migrated canister from its original subnet, updating the registry routing table, and restoring controllers on the replaced canister. [5](#0-4) [6](#0-5) 

### Impact Explanation
When an operator calls `disable_api` to perform an emergency halt (e.g., after discovering a bug in the migration pipeline), any request already in the `REQUESTS` store continues advancing through every stage of the pipeline. The operator has no mechanism to stop in-flight migrations short of upgrading or stopping the canister itself. Concrete consequences include:

- The migration canister becomes the exclusive controller of both the migrated and replaced canisters, locking out their original controllers.
- The migrated canister is deleted from its original subnet.
- The registry routing table is mutated, redirecting traffic.

These are protocol-level, cross-subnet state changes that cannot be trivially reversed.

### Likelihood Explanation
Any canister controller can submit a `migrate_canister` request. The migration pipeline has up to 50 concurrent in-flight requests (`RATE_LIMIT = 50`). An operator disabling the API in response to an incident will find that all requests already accepted continue to completion. The window is not narrow — the pipeline takes multiple timer ticks spanning minutes (the code explicitly waits `MAX_INGRESS_TTL + PERMITTED_DRIFT_AT_VALIDATOR + 30s` in `process_migrated_canister_deleted`), giving ample time for a disable call to arrive mid-flight. [7](#0-6) [8](#0-7) 

### Recommendation
Add a `migrations_disabled()` guard at the top of each processing function (or inside `process_all_by_predicate`). When the flag is set, the function should return `ProcessingResult::NoProgress` without advancing any request. For example:

```rust
pub async fn process_accepted(request: RequestState) -> ProcessingResult<RequestState, RequestState> {
    if migrations_disabled() {
        return ProcessingResult::NoProgress;
    }
    // ... existing logic
}
```

Alternatively, add the check once inside `process_all_by_predicate` before iterating over requests, so all processing paths are covered uniformly. [9](#0-8) 

### Proof of Concept
1. Canister controller calls `migrate_canister({migrated_canister_id, replaced_canister_id})`. The request passes all checks and is inserted as `RequestState::Accepted`.
2. Operator discovers an issue and calls `disable_api()`. `DISABLED` is set to `true`.
3. Timer fires → `process_all_by_predicate("accepted", ...)` runs `process_accepted`. No `migrations_disabled()` check exists. `set_exclusive_controller(migrated_canister)` is called — the migration canister is now the sole controller of the user's canister.
4. Timer fires → `process_controllers_changed` runs. No check. Canister status is verified and the request advances to `StoppedAndReady`.
5. Timer fires → `process_stopped` runs. `rename_canister` is called, mutating the registry.
6. Timer fires → `process_renamed` runs. `migrate_canister` (registry call) is made, updating the routing table.
7. Timer fires → `process_routing_table` runs. `delete_canister` is called — the migrated canister is permanently deleted from its original subnet.

All of this occurs despite `migrations_disabled()` returning `true` throughout, because none of the processing functions check it. [10](#0-9) [11](#0-10) [12](#0-11)

### Citations

**File:** rs/migration_canister/src/migration_canister.rs (L62-65)
```rust
async fn migrate_canister(args: MigrateCanisterArgs) -> Result<(), Option<ValidationError>> {
    if migrations_disabled() {
        return Err(Some(ValidationError::MigrationsDisabled(Reserved)));
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

**File:** rs/migration_canister/src/processing.rs (L111-197)
```rust
pub async fn process_controllers_changed(
    request: RequestState,
) -> ProcessingResult<RequestState, RequestState> {
    let RequestState::ControllersChanged { request } = request else {
        println!("Error: list_by ControllersChanged returned bad variant");
        return ProcessingResult::NoProgress;
    };

    // These checks are repeated because the canisters may have changed since validation:
    let ProcessingResult::Success(migrated_canister_status) =
        canister_status(request.migrated_canister).await
    else {
        return ProcessingResult::NoProgress;
    };
    if migrated_canister_status.status != CanisterStatusType::Stopped {
        return ProcessingResult::FatalFailure(RequestState::Failed {
            request,
            recovery_state: RecoveryState::new(),
            reason: "Migrated canister is not stopped.".to_string(),
        });
    }
    if !migrated_canister_status.ready_for_migration {
        return ProcessingResult::FatalFailure(RequestState::Failed {
            request,
            recovery_state: RecoveryState::new(),
            reason: "Migrated canister is not ready for migration.".to_string(),
        });
    }
    let canister_version = migrated_canister_status.version;
    if canister_version > u64::MAX / 2 {
        return ProcessingResult::FatalFailure(RequestState::Failed {
            request,
            recovery_state: RecoveryState::new(),
            reason: "Migrated canister version is too large.".to_string(),
        });
    }

    let ProcessingResult::Success(replaced_canister_status) =
        canister_status(request.replaced_canister).await
    else {
        return ProcessingResult::NoProgress;
    };
    if replaced_canister_status.status != CanisterStatusType::Stopped {
        return ProcessingResult::FatalFailure(RequestState::Failed {
            request,
            recovery_state: RecoveryState::new(),
            reason: "Replaced canister is not stopped.".to_string(),
        });
    }
    match assert_no_snapshots(request.replaced_canister).await {
        ProcessingResult::Success(_) => {}
        ProcessingResult::NoProgress => return ProcessingResult::NoProgress,
        ProcessingResult::FatalFailure(_) => {
            return ProcessingResult::FatalFailure(RequestState::Failed {
                request,
                recovery_state: RecoveryState::new(),
                reason: "Replaced canister has snapshots.".to_string(),
            });
        }
    }

    if migrated_canister_status.cycles < CYCLES_COST_PER_MIGRATION {
        return ProcessingResult::FatalFailure(RequestState::Failed {
            request,
            recovery_state: RecoveryState::new(),
            reason: format!(
                "Migrated canister does not have sufficient cycles: {} < {}.",
                migrated_canister_status.cycles, CYCLES_COST_PER_MIGRATION
            ),
        });
    }

    // Determine history length of migrated canister
    get_canister_info(request.migrated_canister)
        .await
        .map_success(|canister_info_result| RequestState::StoppedAndReady {
            request: request.clone(),
            stopped_since: time(),
            canister_version,
            canister_history_total_num: canister_info_result.total_num_changes,
        })
        .map_failure(|()| RequestState::Failed {
            request,
            recovery_state: RecoveryState::new(),
            reason: "Migrated canister has been deleted".to_string(),
        })
}
```

**File:** rs/migration_canister/src/processing.rs (L199-229)
```rust
pub async fn process_stopped(
    request: RequestState,
) -> ProcessingResult<
    RequestState,
    RequestState, /* Should be `Infallible` but we want `transition` to be available */
> {
    let RequestState::StoppedAndReady {
        request,
        stopped_since,
        canister_version,
        canister_history_total_num,
    } = request
    else {
        println!("Error: list_by StoppedAndReady returned bad variant");
        return ProcessingResult::NoProgress;
    };
    rename_canister(
        request.migrated_canister,
        canister_version,
        request.replaced_canister,
        request.replaced_canister_subnet,
        canister_history_total_num,
        request.caller,
    )
    .await
    .map_success(|_| RequestState::RenamedReplacedCanister {
        request,
        stopped_since,
    })
    .or_retry()
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

**File:** rs/migration_canister/src/processing.rs (L253-307)
```rust
pub async fn process_updated(
    request: RequestState,
) -> ProcessingResult<RequestState, RequestState> {
    let RequestState::UpdatedRoutingTable {
        request,
        stopped_since,
        registry_version,
    } = request
    else {
        println!("Error: list_by UpdatedRoutingTable returned bad variant");
        return ProcessingResult::NoProgress;
    };
    // call both subnets
    let ProcessingResult::Success(migrated_canister_subnet_version) =
        get_registry_version(request.migrated_canister_subnet).await
    else {
        return ProcessingResult::NoProgress;
    };
    let ProcessingResult::Success(replaced_canister_subnet_version) =
        get_registry_version(request.replaced_canister_subnet).await
    else {
        return ProcessingResult::NoProgress;
    };
    if migrated_canister_subnet_version.is_some_and(|v| v < registry_version)
        || replaced_canister_subnet_version.is_some_and(|v| v < registry_version)
    {
        return ProcessingResult::NoProgress;
    }
    ProcessingResult::Success(RequestState::RoutingTableChangeAccepted {
        request,
        stopped_since,
    })
}

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

**File:** rs/migration_canister/src/processing.rs (L326-334)
```rust
    // and replaced canister subnet that we bound by 30 seconds.
    let max_subnet_clock_drift_nanos = 30 * 1_000_000_000;
    if time().saturating_sub(stopped_since)
        < MAX_INGRESS_TTL.as_nanos() as u64
            + PERMITTED_DRIFT_AT_VALIDATOR.as_nanos() as u64
            + max_subnet_clock_drift_nanos
    {
        return ProcessingResult::NoProgress;
    }
```

**File:** rs/migration_canister/src/canister_state.rs (L56-58)
```rust
pub fn migrations_disabled() -> bool {
    DISABLED.with_borrow(|x| *x.get())
}
```

**File:** rs/migration_canister/src/privileged.rs (L34-38)
```rust
#[update]
fn disable_api() -> Result<(), Option<MigrationCanisterError>> {
    check_caller()?;
    set_disabled_flag(true);
    Ok(())
```

**File:** rs/migration_canister/src/lib.rs (L40-40)
```rust
const RATE_LIMIT: u64 = 50;
```
