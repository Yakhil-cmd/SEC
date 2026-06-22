### Title
Snapshot Griefing DoS Permanently Blocks Canister Migration via TOCTOU in `process_controllers_changed` - (`rs/migration_canister/src/processing.rs`)

---

### Summary

The Migration Canister enforces a "replaced canister must have no snapshots" invariant at two points: once during upfront validation (`validate_request`) and again inside the processing state machine (`process_controllers_changed`). Between these two checks there is a time-of-check/time-of-use (TOCTOU) window during which any controller of the replaced canister can call `take_canister_snapshot` on it. When the processing step subsequently re-checks for snapshots and finds one, it transitions the migration into a permanent `Failed` state, effectively DoS-ing the migration for that canister pair.

---

### Finding Description

**Step 1 – Upfront validation check (passes cleanly):**

`validate_request` in `rs/migration_canister/src/validation.rs` performs check #10:

```rust
// 10. Does the replaced canister have snapshots?
assert_no_snapshots(replaced_canister).await.into_result(
    "Call to management canister `list_canister_snapshots` failed. Try again later.",
)?;
```

If no snapshot exists at this moment, validation succeeds and the `Request` is inserted into `REQUESTS` with state `Accepted`. [1](#0-0) 

**Step 2 – Attack window opens:**

After `migrate_canister` returns `Ok(())`, the request sits in the `Accepted` state until the timer fires and `process_accepted` runs. During this window the original controllers of the replaced canister still have full control over it (the migration canister has not yet called `set_exclusive_controller`). Any controller of the replaced canister can call the management canister's `take_canister_snapshot` on it. [2](#0-1) 

**Step 3 – Processing re-check triggers fatal failure:**

When the timer fires and `process_controllers_changed` runs, it calls `assert_no_snapshots` again:

```rust
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
```

Because the snapshot now exists, this returns `FatalFailure`, permanently transitioning the migration into the `Failed` state. [3](#0-2) 

**`assert_no_snapshots` implementation:**

```rust
pub async fn assert_no_snapshots(canister_id: Principal) -> ProcessingResult<(), ValidationError> {
    match list_canister_snapshots(&ListCanisterSnapshotsArgs { canister_id }).await {
        Ok(snapshots) if snapshots.is_empty() => ProcessingResult::Success(()),
        Ok(_) => {
            ProcessingResult::FatalFailure(ValidationError::ReplacedCanisterHasSnapshots(Reserved))
        }
        ...
    }
}
``` [4](#0-3) 

**The `rename_canister` execution-layer enforcement also blocks on snapshots:**

Even if the processing state machine were patched, the execution environment independently rejects `rename_canister` if the canister has snapshots:

```rust
if canister.canister_snapshots.len() > 0 {
    return Err(CanisterManagerError::RenameCanisterHasSnapshot(old_id));
}
``` [5](#0-4) 

---

### Impact Explanation

A controller of the replaced canister who opposes the migration can:

1. Monitor the Migration Canister for accepted `migrate_canister` calls (observable via `migration_status` query).
2. Immediately call `take_canister_snapshot` on the replaced canister before the timer fires.
3. The migration enters `Failed` permanently; the migration canister attempts controller recovery but the migration does not complete.

If the attacker repeats this every time the legitimate user retries, the replaced canister can never be migrated. The legitimate user cannot remove the attacker as a controller of the replaced canister without first completing the migration (a circular dependency), because the migration canister requires the caller to remain a controller throughout.

The `Failed` state is terminal for that specific migration request. A new `migrate_canister` call must be submitted, giving the attacker another opportunity to repeat the attack. [6](#0-5) 

---

### Likelihood Explanation

- **Attacker entry path**: Any controller of the replaced canister. The management canister's `take_canister_snapshot` is callable by any controller; no special privilege beyond controller status is required.
- **Timing**: The attack window is the interval between `migrate_canister` returning `Ok` and the timer-driven `process_accepted` executing (seconds to tens of seconds). This is observable and actionable.
- **Motivation**: A co-controller who disagrees with the migration decision, or a malicious party who was granted controller status, can permanently block migration.
- **Repeatability**: The attack can be repeated on every retry at zero cost (snapshot storage is bounded and the attacker can delete and re-create).

---

### Recommendation

1. **Delete all snapshots of the replaced canister as part of `process_accepted`** (before or immediately after `set_exclusive_controller`). Once the migration canister is the exclusive controller, it can call `delete_canister_snapshot` on any existing snapshots, eliminating the TOCTOU window entirely.

2. **Alternatively**, remove the redundant `assert_no_snapshots` call from `process_controllers_changed`. The check in `validate_request` is sufficient for early rejection; the processing step should instead proactively delete snapshots rather than fatally failing on their presence.

3. **At minimum**, downgrade the `FatalFailure` in `process_controllers_changed` to `NoProgress` for the snapshot case, so the migration retries rather than permanently failing, and pair it with an automatic snapshot deletion attempt.

---

### Proof of Concept

**Attacker-controlled entry path** (unprivileged ingress to management canister `take_canister_snapshot`, callable by any controller of the replaced canister):

```
1. User A (controller of both canisters) calls migrate_canister(migrated, replaced).
   → validate_request passes (no snapshots exist).
   → Request inserted as RequestState::Accepted.
   → migrate_canister returns Ok(()).

2. Attacker B (another controller of `replaced`) immediately calls:
   management_canister.take_canister_snapshot({ canister_id: replaced, replace_snapshot: None })

3. Timer fires → process_accepted runs → set_exclusive_controller(replaced) succeeds
   (snapshot already exists, but set_exclusive_controller does not check for snapshots).

4. Timer fires → process_controllers_changed runs → assert_no_snapshots(replaced) finds snapshot
   → ProcessingResult::FatalFailure → RequestState::Failed { reason: "Replaced canister has snapshots." }

5. Migration is permanently failed. User A must delete the snapshot and resubmit,
   but Attacker B repeats step 2 on every retry.
```

The production code path is:
- `rs/migration_canister/src/migration_canister.rs`: `migrate_canister` → `validate_request` (passes)
- `rs/migration_canister/src/processing.rs`: `process_controllers_changed` → `assert_no_snapshots` → `FatalFailure`
- `rs/migration_canister/src/external_interfaces/management.rs`: `assert_no_snapshots` [7](#0-6) [8](#0-7)

### Citations

**File:** rs/migration_canister/src/validation.rs (L70-158)
```rust
pub async fn validate_request(
    migrated_canister: Principal,
    replaced_canister: Principal,
    caller: Principal,
) -> Result<(Request, Vec<CanisterGuard>), ValidationError> {
    // 1. The migrated canister must not be equal to the replaced canister.
    if migrated_canister == replaced_canister {
        return Err(ValidationError::SameSubnet(Reserved));
    }

    // The scope ensures that `migrated_canister_subnet` and `replaced_canister_subnet`
    // cannot be used below (step 6 re-fetches the subnets after acquiring the locks).
    {
        // 2. Are the migrated and replaced canisters on cloud engine subnets?
        // It is safe to perform this check before acquiring locks because the fact that
        // neither the migrated nor the replaced canister is on a cloud engine subnet
        // cannot change later.
        let migrated_canister_subnet = get_subnet_for_canister(migrated_canister).await?;
        let replaced_canister_subnet = get_subnet_for_canister(replaced_canister).await?;
        if get_subnet(migrated_canister_subnet).await? == SubnetType::CloudEngine {
            return Err(ValidationError::CloudEngineSubnet {
                subnet: migrated_canister_subnet,
            });
        }
        if get_subnet(replaced_canister_subnet).await? == SubnetType::CloudEngine {
            return Err(ValidationError::CloudEngineSubnet {
                subnet: replaced_canister_subnet,
            });
        }
    }

    // We check if the caller is authorized (i.e.,
    // if the caller is a controller of both the migrated and replaced canisters)
    // before acquiring locks for the migrated and replaced canisters
    // to prevent unauthorized callers from acquiring the lock
    // and blocking authorized callers from performing canister migration.

    // 3. Is the caller controller of the migrated canister?
    let migrated_canister_status =
        check_controllers_and_get_status(migrated_canister, caller).await?;
    // 4. Is the caller controller of the replaced canister?
    let replaced_canister_status =
        check_controllers_and_get_status(replaced_canister, caller).await?;

    // Now we can acquire the locks
    // to prevent reentrancy bugs across asynchronous calls
    // while validating the migrated and replaced canisters.
    let Ok(migrated_canister_guard) = CanisterGuard::new(migrated_canister) else {
        return Err(ValidationError::ValidationInProgress {
            canister: migrated_canister,
        });
    };
    let Ok(replaced_canister_guard) = CanisterGuard::new(replaced_canister) else {
        return Err(ValidationError::ValidationInProgress {
            canister: replaced_canister,
        });
    };

    // 5. Is any of these canisters already in a migration process?
    for request in list_by(|_| true) {
        if let Some(id) = request
            .request()
            .affects_canister(migrated_canister, replaced_canister)
        {
            return Err(ValidationError::MigrationInProgress { canister: id });
        }
    }
    // 6. Are the migrated and replaced canisters on the same subnet?
    let migrated_canister_subnet = get_subnet_for_canister(migrated_canister).await?;
    let replaced_canister_subnet = get_subnet_for_canister(replaced_canister).await?;
    if migrated_canister_subnet == replaced_canister_subnet {
        return Err(ValidationError::SameSubnet(Reserved));
    }
    // 7. Is the migrated canister stopped?
    if migrated_canister_status.status != CanisterStatusType::Stopped {
        return Err(ValidationError::MigratedCanisterNotStopped(Reserved));
    }
    // 8. Is the migrated canister ready for migration?
    if !migrated_canister_status.ready_for_migration {
        return Err(ValidationError::MigratedCanisterNotReady(Reserved));
    }
    // 9. Is the replaced canister stopped?
    if replaced_canister_status.status != CanisterStatusType::Stopped {
        return Err(ValidationError::ReplacedCanisterNotStopped(Reserved));
    }
    // 10. Does the replaced canister have snapshots?
    assert_no_snapshots(replaced_canister).await.into_result(
        "Call to management canister `list_canister_snapshots` failed. Try again later.",
    )?;
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

**File:** rs/migration_canister/src/external_interfaces/management.rs (L265-288)
```rust
pub async fn assert_no_snapshots(canister_id: Principal) -> ProcessingResult<(), ValidationError> {
    match list_canister_snapshots(&ListCanisterSnapshotsArgs { canister_id }).await {
        Ok(snapshots) if snapshots.is_empty() => ProcessingResult::Success(()),
        Ok(_) => {
            ProcessingResult::FatalFailure(ValidationError::ReplacedCanisterHasSnapshots(Reserved))
        }
        Err(CallError::CallRejected(e))
            if e.reject_code() == Ok(RejectCode::DestinationInvalid) =>
        {
            println!(
                "Call `list_canister_snapshots` for {} returned DestinationInvalid, treating as success",
                canister_id
            );
            ProcessingResult::Success(())
        }
        Err(e) => {
            println!(
                "Call `list_canister_snapshots` for {} failed: {:?}",
                canister_id, e
            );
            ProcessingResult::NoProgress
        }
    }
}
```

**File:** rs/execution_environment/src/canister_manager.rs (L2979-2981)
```rust
        if canister.canister_snapshots.len() > 0 {
            return Err(CanisterManagerError::RenameCanisterHasSnapshot(old_id));
        }
```

**File:** rs/migration_canister/src/lib.rs (L46-84)
```rust
#[derive(Clone, Display, Debug, CandidType, Deserialize)]
pub enum ValidationError {
    MigrationsDisabled(Reserved),
    RateLimited(Reserved),
    #[strum(to_string = "ValidationError::ValidationInProgress {{ canister: {canister} }}")]
    ValidationInProgress {
        canister: Principal,
    },
    #[strum(to_string = "ValidationError::MigrationInProgress {{ canister: {canister} }}")]
    MigrationInProgress {
        canister: Principal,
    },
    #[strum(to_string = "ValidationError::CanisterNotFound {{ canister: {canister} }}")]
    CanisterNotFound {
        canister: Principal,
    },
    SameSubnet(Reserved),
    #[strum(to_string = "ValidationError::CallerNotController {{ canister: {canister} }}")]
    CallerNotController {
        canister: Principal,
    },
    #[strum(to_string = "ValidationError::NotController {{ canister: {canister} }}")]
    NotController {
        canister: Principal,
    },
    MigratedCanisterNotStopped(Reserved),
    MigratedCanisterNotReady(Reserved),
    ReplacedCanisterNotStopped(Reserved),
    ReplacedCanisterHasSnapshots(Reserved),
    MigratedCanisterInsufficientCycles(Reserved),
    #[strum(to_string = "ValidationError::CloudEngineSubnet {{ subnet: {subnet} }}")]
    CloudEngineSubnet {
        subnet: Principal,
    },
    #[strum(to_string = "ValidationError::CallFailed {{ reason: {reason} }}")]
    CallFailed {
        reason: String,
    },
}
```
