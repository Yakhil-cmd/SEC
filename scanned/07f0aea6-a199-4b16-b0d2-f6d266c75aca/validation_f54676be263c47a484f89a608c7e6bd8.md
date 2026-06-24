### Title
`get_running_version()` Fails Entirely When Any Single Archive Canister Status Is Unavailable, Permanently Blocking SNS Upgrade Verification — (File: `rs/sns/governance/src/sns_upgrade.rs`)

---

### Summary

`get_running_version()` in the SNS Governance canister iterates over all registered ledger archive canisters and requires every one of them to return a valid status. If any single archive canister's status is unavailable (e.g., the canister is frozen due to cycle depletion, stopped, or the management canister call fails for any reason), the entire function returns an error. This error propagates to `check_upgrade_status()`, which is the periodic task responsible for confirming that an SNS upgrade completed successfully. If the error persists beyond the 5-minute `mark_failed_at_seconds` deadline, the upgrade is permanently marked as failed — even if the actual canister upgrade succeeded — leaving the SNS in an inconsistent state where the recorded `deployed_version` diverges from the actual running version.

---

### Finding Description

`get_running_version()` in `rs/sns/governance/src/sns_upgrade.rs` calls `sns_canisters_summary()`, which calls `get_sns_canisters_summary` on the SNS Root canister with `update_canister_list: Some(true)`. The root canister then makes parallel management canister calls to retrieve the status of every registered canister, including all archive canisters. [1](#0-0) 

Inside `get_running_version()`, the archive canister hashes are collected using:

```rust
let archive_wasm_hash = archives
    .into_iter()
    .map(|canister_summary| get_hash(canister_summary, "Ledger Archive"))
    .collect::<Result<Vec<_>, _>>()?
``` [2](#0-1) 

The `get_hash` helper returns `Err("{label} had no status")` whenever `canister_summary.status` is `None`. The `?` operator then propagates this error, causing `get_running_version()` to return `Err` for the entire call.

`get_owned_canister_summary()` in the SNS Root library, which is called for each archive canister, silently returns `CanisterSummary { status: None }` on any management canister call failure rather than propagating the error: [3](#0-2) 

The root canister's `get_sns_canisters_summary` uses `join_all` over the full dynamic list of archive canister IDs: [4](#0-3) 

The error from `get_running_version()` is handled in `check_upgrade_status()` in `rs/sns/governance/src/governance.rs`: [5](#0-4) 

If `self.env.now() > mark_failed_at` (set to `now + 5 * 60` at upgrade initiation), the upgrade is permanently marked as failed via `complete_sns_upgrade_to_next_version()`, which clears `pending_version` without updating `deployed_version` to the new version: [6](#0-5) 

---

### Impact Explanation

When an archive canister is unavailable during the 5-minute upgrade verification window:

1. `check_upgrade_status()` repeatedly fails to confirm the upgrade.
2. After `mark_failed_at_seconds`, the upgrade proposal is marked as **failed** even though the target canister was actually upgraded successfully.
3. `deployed_version` is **not** updated to the new version.
4. The SNS governance state now records a `deployed_version` that does not match the actual running code.
5. Subsequent `UpgradeSnsToNextVersion` proposals will attempt to upgrade from the stale recorded version, potentially re-upgrading already-upgraded canisters or failing to find a valid upgrade path.
6. The automatic upgrade advancement mechanism (`initiate_upgrade_if_sns_behind_target_version`) will also be confused by the stale `deployed_version`, potentially causing repeated failed upgrade attempts.

This permanently disrupts the SNS upgrade governance mechanism for the affected SNS instance. [7](#0-6) 

---

### Likelihood Explanation

Archive canisters are created automatically by the ICRC-1 ledger when it needs to archive blocks. They are not directly managed by SNS governance in terms of cycle top-ups. An SNS instance with active trading volume will accumulate archive canisters over time. If any archive canister's cycle balance drops below its freezing threshold (a realistic operational scenario for long-running SNS instances), the management canister's `canister_status` call for that archive will fail, returning `None` status.

An unprivileged attacker can accelerate this by submitting many small transactions to the SNS ledger, forcing the creation of archive canisters and increasing their operational costs. The attacker does not need any special permissions — the SNS ledger's `icrc1_transfer` endpoint is publicly accessible.

The 5-minute `mark_failed_at_seconds` window is short enough that a single frozen archive canister during an upgrade window is sufficient to trigger the failure. [8](#0-7) 

---

### Recommendation

1. **Tolerate missing archive statuses in `get_running_version()`**: Instead of failing the entire function when any archive canister's status is `None`, treat archives with missing status as "unknown" and skip them for the version hash comparison, or return a sentinel value. The existing logic already handles the case of zero archives gracefully (`unwrap_or_default()`); it should similarly handle the case where some archives have no status.

2. **Separate archive status from upgrade verification**: The upgrade verification for non-archive canisters (root, governance, ledger, index, swap) should not be blocked by archive canister availability. Archive canister upgrades can be verified independently.

3. **Extend `mark_failed_at_seconds`**: The 5-minute timeout is very short for a system that depends on inter-canister calls that may be temporarily unavailable. A longer timeout (e.g., 30 minutes) would reduce false failure marking.

---

### Proof of Concept

1. An SNS instance has been running for some time and has accumulated one or more ledger archive canisters.
2. An archive canister's cycle balance drops below its freezing threshold (naturally or via attacker-induced transaction volume).
3. SNS governance executes an `UpgradeSnsToNextVersion` proposal, kicking off an upgrade and setting `pending_version` with `mark_failed_at_seconds = now + 300`.
4. `run_periodic_tasks()` calls `check_upgrade_status()`, which calls `get_running_version()`.
5. `get_running_version()` calls `get_sns_canisters_summary` with `update_canister_list: true`.
6. The SNS Root canister calls `management_canister.canister_status(frozen_archive_id)`, which fails; `get_owned_canister_summary` returns `CanisterSummary { status: None }`.
7. `get_running_version()` hits `get_hash(archive_summary, "Ledger Archive")` → `Err("Ledger Archive had no status")` → `?` propagates → returns `Err(...)`.
8. `check_upgrade_status()` logs the error and returns early without confirming the upgrade.
9. After 5 minutes, `self.env.now() > mark_failed_at` is true; `complete_sns_upgrade_to_next_version()` is called with `deployed_version: None`, marking the proposal as failed and clearing `pending_version` without updating `deployed_version`.
10. The SNS's `deployed_version` now records the pre-upgrade version, while the actual canister is running the new code. Future upgrade proposals are confused or blocked. [9](#0-8) [10](#0-9)

### Citations

**File:** rs/sns/governance/src/sns_upgrade.rs (L190-241)
```rust
pub(crate) async fn get_running_version(
    env: &dyn Environment,
    root_canister_id: CanisterId,
) -> Result<Version, String> {
    let response = sns_canisters_summary(env, root_canister_id).await?;

    let GetSnsCanistersSummaryResponse {
        root: Some(root),
        governance: Some(governance),
        ledger: Some(ledger),
        swap: Some(swap),
        dapps: _,
        archives,
        index: Some(index),
    } = response
    else {
        return Err(format!(
            "CanisterSummary could not be fetched for all canisters: {response:?}"
        ));
    };

    let get_hash = |canister_status: CanisterSummary, label: &str| {
        canister_status
            .status
            .ok_or_else(|| format!("{label} had no status"))
            .and_then(|status| {
                status
                    .module_hash
                    .ok_or_else(|| format!("{label} Status had no module hash"))
            })
    };

    // If the values are not all unique, we return vec![0, 0, 0], which will not
    // be interpreted as empty (i.e. no running archives) but won't match any archive hashes
    let archive_wasm_hash = archives
        .into_iter()
        .map(|canister_summary| get_hash(canister_summary, "Ledger Archive"))
        .collect::<Result<Vec<_>, _>>()?
        .into_iter()
        // Make sure all returned versions are the same.
        .reduce(|x, y| if x == y { x } else { vec![0, 0, 0] })
        .unwrap_or_default();

    Ok(Version {
        root_wasm_hash: get_hash(root, "Root")?,
        governance_wasm_hash: get_hash(governance, "Governance")?,
        ledger_wasm_hash: get_hash(ledger, "Ledger")?,
        swap_wasm_hash: get_hash(swap, "Swap")?,
        archive_wasm_hash,
        index_wasm_hash: get_hash(index, "Index")?,
    })
}
```

**File:** rs/sns/root/src/lib.rs (L322-328)
```rust
            join_all(dapp_canister_ids.into_iter().map(|dapp_canister_id| {
                get_owned_canister_summary(management_canister_client, dapp_canister_id)
            })),
            join_all(archive_canister_ids.into_iter().map(|archive_canister_id| {
                get_owned_canister_summary(management_canister_client, archive_canister_id)
            }))
        );
```

**File:** rs/sns/root/src/lib.rs (L1035-1056)
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

    CanisterSummary {
        canister_id: Some(canister_id),
        status,
    }
```

**File:** rs/sns/governance/src/governance.rs (L2894-2898)
```rust
        self.proto.pending_version = Some(PendingVersion {
            target_version: Some(next_version),
            mark_failed_at_seconds: self.env.now() + 5 * 60,
            checking_upgrade_lock: 0,
            proposal_id: Some(proposal_id),
```

**File:** rs/sns/governance/src/governance.rs (L6095-6107)
```rust
    /// Checks if there is a pending upgrade.
    fn should_check_upgrade_status(&self) -> bool {
        self.proto.pending_version.is_some()
    }

    fn can_finalize_disburse_maturity(&self) -> bool {
        let finalizing_disburse_maturity = self.proto.is_finalizing_disburse_maturity;
        finalizing_disburse_maturity.is_none() || !finalizing_disburse_maturity.unwrap()
    }

    /// Checks if pending upgrade is complete and either updates deployed_version
    /// or clears pending_upgrade if beyond the limit.
    async fn check_upgrade_status(&mut self) {
```

**File:** rs/sns/governance/src/governance.rs (L6173-6204)
```rust
        let running_version: Result<Version, String> =
            get_running_version(&*self.env, self.proto.root_canister_id_or_panic()).await;

        // Mark the check as inactive after async call.
        self.proto
            .pending_version
            .as_mut()
            .unwrap()
            .checking_upgrade_lock = 0;

        // We cannot panic or we will get stuck with "checking_upgrade_lock" set to true.  We log
        // the issue and return so the next check can be performed.
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

**File:** rs/sns/governance/src/governance.rs (L6280-6313)
```rust
    fn complete_sns_upgrade_to_next_version(
        &mut self,
        proposal_id: Option<u64>,
        status: upgrade_journal_entry::upgrade_outcome::Status,
        message: String,
        deployed_version: Option<Version>,
    ) {
        use upgrade_journal_entry::upgrade_outcome::Status;

        let result = match &status {
            Status::Success(_) => Ok(()),
            Status::InvalidState(_) => Err(GovernanceError::new_with_message(
                ErrorType::InconsistentInternalData,
                message.to_string(),
            )),
            Status::ExternalFailure(_) | Status::Timeout(_) => Err(
                GovernanceError::new_with_message(ErrorType::External, message.to_string()),
            ),
        };

        self.push_to_upgrade_journal(upgrade_journal_entry::UpgradeOutcome {
            human_readable: Some(message),
            status: Some(status),
        });

        if let Some(proposal_id) = proposal_id {
            self.set_proposal_execution_status(proposal_id, result);
        }

        self.proto.pending_version = None;

        if let Some(deployed_version) = deployed_version {
            self.proto.deployed_version.replace(deployed_version);
        }
```
