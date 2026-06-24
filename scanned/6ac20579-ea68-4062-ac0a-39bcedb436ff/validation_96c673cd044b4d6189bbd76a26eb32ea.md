### Title
Missing Subnet-Type Compatibility Check in `validate_request` Allows Cross-Type Canister Migration - (File: `rs/migration_canister/src/validation.rs`)

---

### Summary

The migration canister's `validate_request` function only rejects migrations involving `CloudEngine` subnets but does not verify that the source and destination subnets share the same `SubnetType`. The governance-controlled `prepare_canister_migration` path explicitly enforces this via `validate_subnets_consistency`. A canister owner who controls canisters on two subnets of different types (e.g., `Application` vs `VerifiedApplication`, or `Application` vs `System`) can trigger a migration that the protocol was never designed to support, potentially leaving the canister in an operationally incompatible environment with no recourse.

---

### Finding Description

`validate_request` in `rs/migration_canister/src/validation.rs` performs eleven pre-flight checks before accepting a migration request. Step 2 fetches the subnet for each canister and rejects the request if either subnet is `SubnetType::CloudEngine`:

```rust
if get_subnet(migrated_canister_subnet).await? == SubnetType::CloudEngine {
    return Err(ValidationError::CloudEngineSubnet { subnet: migrated_canister_subnet });
}
if get_subnet(replaced_canister_subnet).await? == SubnetType::CloudEngine {
    return Err(ValidationError::CloudEngineSubnet { subnet: replaced_canister_subnet });
}
```

No check is made that `migrated_canister_subnet` and `replaced_canister_subnet` share the same `SubnetType`. The `ValidationError` enum contains no `SubnetTypesMismatch` variant.

By contrast, the governance-owned `prepare_canister_migration` path in `rs/registry/canister/src/mutations/prepare_canister_migration.rs` calls `validate_subnets_consistency`, which enforces both type equality and size equality before any migration is recorded:

```rust
fn validate_subnets_consistency(
    source_subnet: &SubnetRecord,
    destination_subnet: &SubnetRecord,
) -> Result<(), PrepareCanisterMigrationError> {
    if source_subnet.subnet_type != destination_subnet.subnet_type {
        return Err(PrepareCanisterMigrationError::SubnetTypesMismatch(...));
    }
    if source_subnet.membership.len() != destination_subnet.membership.len() {
        return Err(PrepareCanisterMigrationError::SubnetSizesMismatch(...));
    }
    Ok(())
}
```

The migration canister's processing pipeline (`process_accepted` → `process_controllers_changed` → `process_stopped` → `process_renamed` → `process_updated` → `process_routing_table` → `process_migrated_canister_deleted`) never re-checks subnet type compatibility at any later stage either.

---

### Impact Explanation

Once `validate_request` accepts the request, the migration canister irrevocably:
1. Takes exclusive controller of both canisters (`set_exclusive_controller`).
2. Calls `rename_canister` on the destination subnet, atomically re-pointing the migrated canister's ID to the destination.
3. Updates the registry routing table via `migrate_canister` (registry call).
4. Deletes the original canister from the source subnet.
5. Restores controllers on the now-renamed canister on the destination subnet.

After step 4 the original canister is gone. If the destination subnet is of a different type (e.g., `VerifiedApplication` vs `Application`, or `System` vs `Application`), the canister now operates under different execution semantics, cycle-cost schedules, and governance rules than it was designed for. Depending on the type mismatch:

- A canister migrated onto a `System` subnet (NNS) is subject to NNS governance and different cycle accounting; its cycles balance may be drained at a different rate, potentially causing it to be frozen or deleted.
- A canister migrated from a `VerifiedApplication` subnet to an `Application` subnet loses the stronger security guarantees its users relied on.
- In either direction, the canister's original controllers may find the canister in a state they cannot recover from, because the migration canister has already restored controllers and closed the request as `Succeeded`.

---

### Likelihood Explanation

The attacker-controlled entry path is a standard ingress `update` call to `migrate_canister` by any principal that:
1. Is a controller of the `migrated_canister_id` (on subnet type T1).
2. Is a controller of the `replaced_canister_id` (on subnet type T2 ≠ T1).
3. Has previously added the migration canister as a controller of both canisters (required by `check_controllers_and_get_status`).

No privileged role, governance majority, or key material is required beyond ordinary canister controller status. A developer who manages canisters across multiple subnet types — a realistic scenario as the IC grows — can trigger this accidentally. The allowlist (`caller_allowed`) provides a soft rollout gate but is not a permanent security boundary; it is configurable by the migration canister's own upgrade path.

---

### Recommendation

Add a subnet-type compatibility check inside `validate_request` in `rs/migration_canister/src/validation.rs`, immediately after the `CloudEngine` checks (step 2), mirroring `validate_subnets_consistency`:

```rust
let migrated_subnet_type = get_subnet(migrated_canister_subnet).await?;
let replaced_subnet_type = get_subnet(replaced_canister_subnet).await?;
if migrated_subnet_type != replaced_subnet_type {
    return Err(ValidationError::SubnetTypesMismatch {
        migrated_subnet: migrated_canister_subnet,
        replaced_subnet: replaced_canister_subnet,
    });
}
```

Add the corresponding `SubnetTypesMismatch` variant to `ValidationError` in `rs/migration_canister/src/lib.rs`.

---

### Proof of Concept

1. Alice creates canister **A** on an `Application` subnet (e.g., `subnet-app`).
2. Alice creates canister **B** on a `VerifiedApplication` subnet (e.g., `subnet-vapp`).
3. Alice adds the migration canister as a controller of both **A** and **B**, and stops both.
4. Alice calls `migrate_canister { migrated_canister_id: A, replaced_canister_id: B }`.
5. `validate_request` fetches both subnets, confirms neither is `CloudEngine`, and proceeds — no type-equality check is performed.
6. The migration state machine runs to completion: **A**'s state is renamed to **B**'s ID on `subnet-vapp`, the routing table is updated, and the original **A** on `subnet-app` is deleted.
7. **A**'s canister ID now resolves to `subnet-vapp` (a `VerifiedApplication` subnet), operating under different rules than intended, with no way to reverse the operation. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/migration_canister/src/validation.rs (L82-99)
```rust
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
```

**File:** rs/registry/canister/src/mutations/prepare_canister_migration.rs (L164-183)
```rust
fn validate_subnets_consistency(
    source_subnet: &SubnetRecord,
    destination_subnet: &SubnetRecord,
) -> Result<(), PrepareCanisterMigrationError> {
    if source_subnet.subnet_type != destination_subnet.subnet_type {
        return Err(PrepareCanisterMigrationError::SubnetTypesMismatch(
            SubnetType::try_from(source_subnet.subnet_type).ok(),
            SubnetType::try_from(destination_subnet.subnet_type).ok(),
        ));
    }

    if source_subnet.membership.len() != destination_subnet.membership.len() {
        return Err(PrepareCanisterMigrationError::SubnetSizesMismatch(
            source_subnet.membership.len(),
            destination_subnet.membership.len(),
        ));
    }

    Ok(())
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
