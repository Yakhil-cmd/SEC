### Title
`assert_no_snapshots()` Silently Treats `DestinationInvalid` as "No Snapshots" for Cross-Subnet Canisters, Bypassing the Snapshot Safety Check - (File: `rs/migration_canister/src/external_interfaces/management.rs`)

---

### Summary

`assert_no_snapshots()` in the migration canister calls `list_canister_snapshots` on the replaced canister and, when the management canister returns `DestinationInvalid`, unconditionally treats this as `ProcessingResult::Success(())` — i.e., "no snapshots, proceed with migration." Because the replaced canister is always on a **different subnet** from the migration canister (this is the entire purpose of the migration canister), the local management canister cannot enumerate its snapshots and always returns `DestinationInvalid`. The snapshot guard is therefore a dead letter for every real cross-subnet migration: a replaced canister with existing snapshots will never trigger `ValidationError::ReplacedCanisterHasSnapshots`, and migration proceeds unconditionally, destroying those snapshots.

---

### Finding Description

`assert_no_snapshots` is called in two places:

1. **Validation phase** — `validate_request()` step 10 in `rs/migration_canister/src/validation.rs` line 156.
2. **Processing phase** — `process_controllers_changed()` in `rs/migration_canister/src/processing.rs` line 160.

The function body is:

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
            ProcessingResult::Success(())   // ← snapshot check silently bypassed
        }
        Err(e) => { ... ProcessingResult::NoProgress }
    }
}
``` [1](#0-0) 

`list_canister_snapshots` is a management-canister method that only has visibility into canisters on the **local subnet**. When the target canister lives on a remote subnet, the local management canister returns `DestinationInvalid`. Because `validate_request` step 6 explicitly requires the migrated and replaced canisters to be on **different** subnets:

```rust
if migrated_canister_subnet == replaced_canister_subnet {
    return Err(ValidationError::SameSubnet(Reserved));
}
``` [2](#0-1) 

…the replaced canister is **always** on a different subnet. Therefore `list_canister_snapshots` for the replaced canister **always** returns `DestinationInvalid`, and `assert_no_snapshots` **always** returns `Success(())` regardless of how many snapshots the replaced canister actually holds.

The same bypass occurs in the processing state machine:

```rust
match assert_no_snapshots(request.replaced_canister).await {
    ProcessingResult::Success(_) => {}   // always taken for cross-subnet
    ...
}
``` [3](#0-2) 

The analog to the original report is exact: just as `OperateProxy.callFunction()` performs a low-level call to a non-contract address and treats the empty-success response as a real success, `assert_no_snapshots` performs a management-canister call to a cross-subnet canister and treats the `DestinationInvalid` rejection as "no snapshots found."

---

### Impact Explanation

The snapshot check is the only guard preventing migration of a replaced canister that has live snapshots. When bypassed:

- All snapshots stored on the replaced canister are silently destroyed during migration, with no warning to the caller.
- The `ValidationError::ReplacedCanisterHasSnapshots` error variant is unreachable in any real cross-subnet migration scenario.
- The processing-phase re-check in `process_controllers_changed` (which is supposed to catch post-validation snapshot creation) is equally ineffective.

The test `validation_fails_snapshot` only passes because PocketIC runs both canisters on the same subnet, making `list_canister_snapshots` succeed locally — a condition that never holds in production. [4](#0-3) 

---

### Likelihood Explanation

Every production invocation of `migrate_canister` involves a replaced canister on a different subnet (same-subnet calls are rejected). Therefore the bypass is triggered on **every** migration attempt. Any caller who is a controller of both canisters and whose replaced canister has snapshots will silently lose those snapshots. The entry path requires no special privilege beyond being a controller of both canisters, which is the normal caller role for this API. [5](#0-4) 

---

### Recommendation

Replace the `DestinationInvalid → Success` mapping with a proper cross-subnet snapshot query. The migration canister should call `list_canister_snapshots` on the **replaced canister's own subnet** management canister (using `Call::bounded_wait(replaced_canister_subnet, "list_canister_snapshots")`) rather than the local management canister, mirroring the pattern already used for `delete_canister` and `rename_canister`:

```rust
match Call::bounded_wait(replaced_canister_subnet, "list_canister_snapshots")
    .with_arg(&ListCanisterSnapshotsArgs { canister_id })
    .await
{
    Ok(response) => {
        let snapshots = response.candid::<Vec<Snapshot>>()?;
        if snapshots.is_empty() { Success(()) } else { FatalFailure(...) }
    }
    Err(...) => NoProgress,
}
```

The `replaced_canister_subnet` is already available in the `Request` struct at both call sites. [6](#0-5) 

---

### Proof of Concept

1. Caller controls `migrated_canister` (on subnet A) and `replaced_canister` (on subnet B).
2. Caller takes a snapshot of `replaced_canister`: `take_canister_snapshot(replaced_canister, ...)`.
3. Caller stops both canisters and calls `migrate_canister({ migrated_canister_id, replaced_canister_id })`.
4. `validate_request` step 10 calls `assert_no_snapshots(replaced_canister)`.
5. The local management canister (on the migration canister's subnet) returns `DestinationInvalid` for `replaced_canister` because it is on subnet B.
6. `assert_no_snapshots` maps `DestinationInvalid` → `Success(())`.
7. `validate_request` returns `Ok(...)` and the migration is accepted.
8. `process_controllers_changed` repeats the same `assert_no_snapshots` call with the same result.
9. Migration proceeds to completion; the snapshot on `replaced_canister` is permanently destroyed with no error reported to the caller. [7](#0-6) [8](#0-7)

### Citations

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

**File:** rs/migration_canister/src/external_interfaces/management.rs (L355-381)
```rust
pub async fn delete_canister(
    canister_id: Principal,
    subnet_id: Principal,
) -> ProcessingResult<(), Infallible> {
    let args = DeleteCanisterArgs { canister_id };
    match Call::bounded_wait(subnet_id, "delete_canister")
        .with_arg(&args)
        .await
    {
        Ok(_) => ProcessingResult::Success(()),
        Err(e) => {
            println!(
                "Call `delete_canister` for canister: {}, subnet: {}, failed: {:?}",
                canister_id, subnet_id, e
            );
            match e {
                CallFailed::CallRejected(e) => {
                    if e.reject_code() == Ok(RejectCode::DestinationInvalid) {
                        ProcessingResult::Success(())
                    } else {
                        ProcessingResult::NoProgress
                    }
                }
                _ => ProcessingResult::NoProgress,
            }
        }
    }
```

**File:** rs/migration_canister/src/validation.rs (L140-142)
```rust
    if migrated_canister_subnet == replaced_canister_subnet {
        return Err(ValidationError::SameSubnet(Reserved));
    }
```

**File:** rs/migration_canister/src/validation.rs (L155-158)
```rust
    // 10. Does the replaced canister have snapshots?
    assert_no_snapshots(replaced_canister).await.into_result(
        "Call to management canister `list_canister_snapshots` failed. Try again later.",
    )?;
```

**File:** rs/migration_canister/src/processing.rs (L160-170)
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

**File:** rs/migration_canister/tests/tests.rs (L1280-1315)
```rust
#[tokio::test]
async fn validation_fails_snapshot() {
    let Setup {
        pic,
        migrated_canisters,
        replaced_canisters,
        replaced_canister_controllers,
        ..
    } = setup(Settings::default()).await;
    let sender = replaced_canister_controllers[0];
    let migrated_canister = migrated_canisters[0];
    let replaced_canister = replaced_canisters[0];
    // install a minimal Wasm module
    pic.install_canister(
        replaced_canister,
        b"\x00\x61\x73\x6d\x01\x00\x00\x00".to_vec(),
        vec![],
        Some(sender),
    )
    .await;
    let _ = pic
        .take_canister_snapshot(replaced_canister, Some(sender), None)
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
        Err(ValidationError::ReplacedCanisterHasSnapshots(Reserved))
    ));
```

**File:** rs/migration_canister/migration_canister.did (L36-37)
```text
  migrate_canister : (MigrateCanisterArgs) -> (ValidationResult);
  migration_status : (MigrateCanisterArgs) -> (opt MigrationStatus) query;
```
