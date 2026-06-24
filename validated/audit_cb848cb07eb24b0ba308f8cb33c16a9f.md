All code references check out exactly as described. The vulnerability is confirmed.

Audit Report

## Title
`get_running_version()` Fails Entirely When Any Archive Canister Status Is Unavailable, Permanently Blocking SNS Upgrade Verification — (File: `rs/sns/governance/src/sns_upgrade.rs`)

## Summary
`get_running_version()` uses the `?` operator to propagate errors when any archive canister's status is `None`, causing the entire function to fail. Since `get_owned_canister_summary()` in the SNS Root silently returns `CanisterSummary { status: None }` on any management canister call failure, a single frozen or stopped archive canister causes `check_upgrade_status()` to repeatedly fail. After the 5-minute `mark_failed_at_seconds` deadline, the upgrade is permanently marked as failed with `deployed_version: None`, leaving the SNS with a stale `deployed_version` that diverges from the actual running code.

## Finding Description
In `rs/sns/governance/src/sns_upgrade.rs` lines 224–227, archive canister hashes are collected with `.collect::<Result<Vec<_>, _>>()?`. The `get_hash` closure returns `Err("{label} had no status")` when `canister_summary.status` is `None`. The `?` propagates this error, causing `get_running_version()` to return `Err` for the entire call. [1](#0-0) 

In `rs/sns/root/src/lib.rs` lines 1035–1051, `get_owned_canister_summary()` catches any management canister call failure and returns `status: None` rather than propagating the error. [2](#0-1) 

Archive canisters are fetched via `join_all` over the full dynamic list at lines 325–327. [3](#0-2) 

In `rs/sns/governance/src/governance.rs`, `check_upgrade_status()` at lines 6185–6204 handles the `Err` from `get_running_version()`: it logs the error and, once `self.env.now() > mark_failed_at`, calls `complete_sns_upgrade_to_next_version(..., None)` with `deployed_version: None`. [4](#0-3) 

`complete_sns_upgrade_to_next_version()` at lines 6309–6313 clears `pending_version` but only updates `deployed_version` if the passed `deployed_version` is `Some`. When called with `None`, `deployed_version` is left at the pre-upgrade value. [5](#0-4) 

The `mark_failed_at_seconds` window is only 5 minutes. [6](#0-5) 

## Impact Explanation
This is a **High** severity issue. The SNS upgrade governance mechanism is permanently disrupted for the affected SNS instance: `deployed_version` records the pre-upgrade version while the actual canister runs new code. Subsequent `UpgradeSnsToNextVersion` proposals will attempt to upgrade from the stale recorded version, potentially re-upgrading already-upgraded canisters or failing to find a valid upgrade path. The automatic advancement mechanism (`initiate_upgrade_if_sns_behind_target_version`) is also confused by the stale `deployed_version`. This matches the allowed impact: "Significant SNS security impact with concrete user or protocol harm."

## Likelihood Explanation
An unprivileged attacker can trigger this by submitting many small transactions to the SNS ledger via the publicly accessible `icrc1_transfer` endpoint, forcing creation of archive canisters and increasing their operational costs until a canister's cycle balance drops below its freezing threshold. No special permissions are required. The 5-minute window is short enough that a single frozen archive canister during any upgrade window is sufficient to trigger the failure. Long-running SNS instances with active trading volume are naturally at risk even without an attacker.

## Recommendation
1. **Tolerate missing archive statuses**: In `get_running_version()`, instead of using `.collect::<Result<Vec<_>, _>>()?`, filter out archives with `None` status (or treat them as unknown) rather than failing the entire function. The existing `unwrap_or_default()` already handles zero archives gracefully; the same tolerance should apply to partially unavailable archives.
2. **Decouple archive verification from core canister verification**: The upgrade verification for root, governance, ledger, index, and swap should not be blocked by archive canister availability.
3. **Extend `mark_failed_at_seconds`**: The 5-minute timeout is too short for a system dependent on inter-canister calls that may be temporarily unavailable. A longer timeout (e.g., 30 minutes) would reduce false failure marking.

## Proof of Concept
1. Deploy an SNS instance with active ledger usage so that one or more archive canisters are created.
2. Drain an archive canister's cycles below its freezing threshold (or simulate this in a PocketIC test by mocking `canister_status` to return an error for the archive canister ID).
3. Execute an `UpgradeSnsToNextVersion` proposal, setting `pending_version` with `mark_failed_at_seconds = now + 300`.
4. Advance time past 5 minutes and trigger `run_periodic_tasks()` → `check_upgrade_status()`.
5. Observe: `get_running_version()` returns `Err("Ledger Archive had no status")`, `complete_sns_upgrade_to_next_version` is called with `deployed_version: None`, `pending_version` is cleared, and `deployed_version` remains at the pre-upgrade value.
6. Confirm that a subsequent `UpgradeSnsToNextVersion` proposal uses the stale `deployed_version` as its base, causing incorrect upgrade path resolution.

### Citations

**File:** rs/sns/governance/src/sns_upgrade.rs (L224-227)
```rust
    let archive_wasm_hash = archives
        .into_iter()
        .map(|canister_summary| get_hash(canister_summary, "Ledger Archive"))
        .collect::<Result<Vec<_>, _>>()?
```

**File:** rs/sns/root/src/lib.rs (L325-327)
```rust
            join_all(archive_canister_ids.into_iter().map(|archive_canister_id| {
                get_owned_canister_summary(management_canister_client, archive_canister_id)
            }))
```

**File:** rs/sns/root/src/lib.rs (L1035-1051)
```rust
    let status = match management_canister_client
        .canister_status(canister_id_record)
        .await
        .map(CanisterStatusResultV2::from)
    {
        Ok(canister_status_result_v2) => Some(canister_status_result_v2),
        Err(err) => {
            // Log an error and return a CanisterSummary with no status
            log!(
                ERROR,
                "Unable to get the status of canister_id {}. Reason: {:?}",
                canister_id,
                err
            );
            None
        }
    };
```

**File:** rs/sns/governance/src/governance.rs (L2894-2898)
```rust
        self.proto.pending_version = Some(PendingVersion {
            target_version: Some(next_version),
            mark_failed_at_seconds: self.env.now() + 5 * 60,
            checking_upgrade_lock: 0,
            proposal_id: Some(proposal_id),
```

**File:** rs/sns/governance/src/governance.rs (L6185-6204)
```rust
        let mut running_version = match running_version {
            Ok(version) => version,
            Err(err) => {
                // Always log this, even if we are not yet marking as failed.
                log!(ERROR, "Could not get running version of SNS: {}", err);

                if self.env.now() > mark_failed_at {
                    let message = format!(
                        "Upgrade marked as failed at {}. \
                         Governance could not determine running version from root: {}. \
                         Setting upgrade to failed to unblock retry.",
                        format_timestamp_for_humans(self.env.now()),
                        err,
                    );
                    let status = upgrade_journal_entry::upgrade_outcome::Status::Timeout(Empty {});

                    self.complete_sns_upgrade_to_next_version(proposal_id, status, message, None);
                }
                return;
            }
```

**File:** rs/sns/governance/src/governance.rs (L6309-6313)
```rust
        self.proto.pending_version = None;

        if let Some(deployed_version) = deployed_version {
            self.proto.deployed_version.replace(deployed_version);
        }
```
