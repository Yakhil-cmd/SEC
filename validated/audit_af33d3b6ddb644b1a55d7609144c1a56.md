### Title
Missing `migrations_disabled` Check in Timer-Driven Processing Pipeline Allows In-Flight Canister Migrations to Complete After Governance Pause - (File: rs/migration_canister/src/processing.rs)

### Summary

The `migrate_canister` ingress endpoint in the Migration Canister checks the `migrations_disabled` flag before accepting a new migration request. However, none of the seven timer-driven processing stages that execute the migration after acceptance re-check this flag. When NNS governance passes a `PauseCanisterMigrations` proposal (calling `disable_api`), already-accepted migrations continue to execute through all stages — changing controllers, renaming canisters, updating routing tables, and deleting canisters — in direct violation of the governance's intent to halt all migration activity.

### Finding Description

**Initiation check (present):**

In `rs/migration_canister/src/migration_canister.rs`, the `migrate_canister` update function checks the disabled flag before accepting any request:

```rust
async fn migrate_canister(args: MigrateCanisterArgs) -> Result<(), Option<ValidationError>> {
    if migrations_disabled() {
        return Err(Some(ValidationError::MigrationsDisabled(Reserved)));
    }
    // ...
    insert_request(RequestState::Accepted { request });
``` [1](#0-0) 

**Completion checks (absent):**

Once a request is inserted into `REQUESTS` as `RequestState::Accepted`, a timer-driven pipeline processes it through seven sequential stages. None of these stages call `migrations_disabled()`:

- `process_accepted()` — sets exclusive controller on both canisters
- `process_controllers_changed()` — verifies canister status and readiness
- `process_stopped()` — calls `rename_canister` on the registry
- `process_renamed()` — calls `migrate_canister` on the registry to update routing table
- `process_updated()` — waits for both subnets to catch up to the new registry version
- `process_routing_table()` — deletes the migrated canister from its original subnet
- `process_migrated_canister_deleted()` — waits for ingress TTL expiry, then restores original controllers [2](#0-1) [3](#0-2) [4](#0-3) 

**The disable mechanism:**

`disable_api` (callable by controllers or NNS governance via `PauseCanisterMigrations`) sets the `DISABLED` stable cell to `true`: [5](#0-4) [6](#0-5) 

The NNS governance wiring confirms `PauseCanisterMigrations` maps directly to `disable_api` on the migration canister: [7](#0-6) 

**Exploit flow:**

1. Unprivileged user calls `migrate_canister(migrated_canister_id, replaced_canister_id)` while migrations are enabled. All validation passes; the request is inserted as `RequestState::Accepted`.
2. NNS governance passes a `PauseCanisterMigrations` proposal (e.g., in response to a discovered vulnerability in the migration process). `DISABLED` is set to `true`.
3. The timer fires and calls `process_all_by_predicate` for each stage. None of the stage processors check `migrations_disabled()`.
4. The migration proceeds through all stages: controllers are changed, the canister is renamed in the registry, the routing table is updated, the original canister is deleted, and controllers are restored — all after the governance-mandated pause. [8](#0-7) 

### Impact Explanation

When NNS governance issues a `PauseCanisterMigrations` proposal — the only mechanism to halt migration activity — the pause is silently ineffective against any migration already in the `REQUESTS` queue. The migration pipeline performs irreversible operations: it takes exclusive control of both canisters, deletes the migrated canister from its original subnet, and updates the global routing table. If the pause was triggered because a bug was discovered in the migration logic, those buggy operations still execute on all in-flight requests. This constitutes a **governance authorization bypass**: the governance's expressed intent to halt all migration activity is not honored for in-flight requests, and the canister state changes (controller reassignment, canister deletion, routing table mutation) are permanent and cannot be undone by simply re-enabling the pause.

### Likelihood Explanation

The window is realistic. The migration pipeline is multi-stage and timer-driven, with each stage requiring inter-canister calls and registry propagation. The `process_migrated_canister_deleted` stage alone waits for `MAX_INGRESS_TTL + PERMITTED_DRIFT_AT_VALIDATOR + 30s` (approximately 5–6 minutes) before restoring controllers. Any migration accepted before a governance pause proposal is executed will remain in-flight for this entire window. Given that governance proposals take time to pass and execute, and that the migration canister is designed to handle multiple concurrent migrations, it is plausible that one or more migrations are in-flight at the moment a pause is enacted. [9](#0-8) 

### Recommendation

Add a `migrations_disabled()` check at the start of each processing stage function. When the flag is set, the stage processor should return `ProcessingResult::NoProgress` (causing the request to remain in the queue without advancing) rather than `ProcessingResult::FatalFailure` (which would trigger recovery). This preserves the ability to resume in-flight migrations after the pause is lifted, while fully honoring the governance's intent to halt all migration activity during the pause period. For example, in `process_accepted`:

```rust
pub async fn process_accepted(request: RequestState) -> ProcessingResult<RequestState, RequestState> {
    if migrations_disabled() {
        return ProcessingResult::NoProgress;
    }
    // ... existing logic
}
```

The same guard should be added to `process_controllers_changed`, `process_stopped`, `process_renamed`, `process_updated`, `process_routing_table`, and `process_migrated_canister_deleted`.

### Proof of Concept

1. Deploy the migration canister with two valid stopped canisters on different subnets.
2. Call `migrate_canister(migrated_canister_id, replaced_canister_id)` — request is accepted.
3. Immediately call `disable_api()` (simulating a governance pause).
4. Observe via `migration_status()` that the migration continues to advance through `ControllersChanged → StoppedAndReady → RenamedReplacedCanister → UpdatedRoutingTable → RoutingTableChangeAccepted → MigratedCanisterDeleted → RestoredControllers → Succeeded` despite `migrations_disabled()` returning `true`.
5. The migration completes fully, demonstrating that the pause had no effect on the in-flight request. [10](#0-9)

### Citations

**File:** rs/migration_canister/src/migration_canister.rs (L62-93)
```rust
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

**File:** rs/migration_canister/src/migration_canister.rs (L105-127)
```rust
#[query]
fn migration_status(args: MigrateCanisterArgs) -> Option<MigrationStatus> {
    if let Some(request_status) = find_request(args.migrated_canister_id, args.replaced_canister_id)
    {
        let migration_status = MigrationStatus::InProgress {
            status: request_status.name().to_string(),
        };
        Some(migration_status)
    } else if let Some(event) =
        find_last_event(args.migrated_canister_id, args.replaced_canister_id)
    {
        let migration_status = match event.event {
            crate::EventType::Succeeded { .. } => MigrationStatus::Succeeded { time: event.time },
            crate::EventType::Failed { reason, .. } => MigrationStatus::Failed {
                reason,
                time: event.time,
            },
        };
        Some(migration_status)
    } else {
        None
    }
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

**File:** rs/migration_canister/src/processing.rs (L199-351)
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

pub async fn process_migrated_canister_deleted(
    request: RequestState,
) -> ProcessingResult<RequestState, RequestState> {
    let RequestState::MigratedCanisterDeleted {
        request,
        stopped_since,
    } = request
    else {
        println!("Error: list_by MigratedCanisterDeleted returned bad variant");
        return ProcessingResult::NoProgress;
    };
    // The protocol ensures the following:
    // "The ingress expiry of an ingress message that is actually executed
    // is never more than `MAX_INGRESS_TTL + PERMITTED_DRIFT_AT_VALIDATOR` into the future
    // w.r.t. the subnet time that executed the ingress message.
    // Hence, we must wait for at least `MAX_INGRESS_TTL + PERMITTED_DRIFT_AT_VALIDATOR`
    // and also additionally account for a clock drift between the migrated canister
    // and replaced canister subnet that we bound by 30 seconds.
    let max_subnet_clock_drift_nanos = 30 * 1_000_000_000;
    if time().saturating_sub(stopped_since)
        < MAX_INGRESS_TTL.as_nanos() as u64
            + PERMITTED_DRIFT_AT_VALIDATOR.as_nanos() as u64
            + max_subnet_clock_drift_nanos
    {
        return ProcessingResult::NoProgress;
    }
    // restore controllers
    let controllers = request
        .migrated_canister_original_controllers
        .iter()
        .filter(|x| **x != canister_self())
        .cloned()
        .collect::<Vec<Principal>>();
    let ProcessingResult::Success(()) =
        // The migration canister is the exclusive controller of `request.migrated_canister`
        // and thus the following call cannot fail because of the caller
        // not being a controller.
        set_controllers(request.migrated_canister, controllers, request.replaced_canister_subnet).await
    else {
        return ProcessingResult::NoProgress;
    };
    ProcessingResult::Success(RequestState::RestoredControllers { request })
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

**File:** rs/migration_canister/src/canister_state.rs (L56-58)
```rust
pub fn migrations_disabled() -> bool {
    DISABLED.with_borrow(|x| *x.get())
}
```

**File:** rs/nns/governance/src/proposals/execute_nns_function.rs (L585-586)
```rust
            ValidNnsFunction::PauseCanisterMigrations => (MIGRATION_CANISTER_ID, "disable_api"),
            ValidNnsFunction::UnpauseCanisterMigrations => (MIGRATION_CANISTER_ID, "enable_api"),
```
