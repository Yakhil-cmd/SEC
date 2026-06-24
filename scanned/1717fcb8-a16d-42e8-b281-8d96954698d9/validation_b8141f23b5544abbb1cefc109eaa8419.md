### Title
Unprivileged Caller Can Abort In-Progress SNS Upgrade via `fail_stuck_upgrade_in_progress` - (File: `rs/sns/governance/canister/canister.rs`)

### Summary

The `fail_stuck_upgrade_in_progress` update endpoint in the SNS Governance canister is exposed publicly with no caller check. Any unprivileged ingress sender can invoke it after an upgrade's `mark_failed_at_seconds` deadline passes to forcibly clear `pending_version` and mark the upgrade proposal as `ExternalFailure`, even when the actual canister upgrade succeeded. This mirrors the original report's pattern: a state-resetting function callable by anyone that disrupts a privileged multi-step operation.

### Finding Description

The canister endpoint is registered as a plain `#[update]` with no access control: [1](#0-0) 

It delegates directly to the governance logic: [2](#0-1) 

When `now > pending_version.mark_failed_at_seconds`, the function calls `complete_sns_upgrade_to_next_version`, which unconditionally:
1. Sets `self.proto.pending_version = None`
2. Marks the associated proposal as failed with `ExternalFailure` [3](#0-2) 

The `mark_failed_at_seconds` deadline is set to only 5 minutes after upgrade kick-off: [4](#0-3) 

The periodic task `check_upgrade_status` is the intended mechanism to confirm upgrade success and clear `pending_version`. However, because `fail_stuck_upgrade_in_progress` is callable by anyone and races against the periodic task, an attacker who calls it immediately after the 5-minute deadline can win the race and mark the proposal as failed before the periodic task confirms success.

### Impact Explanation

An attacker can:
- Force any SNS upgrade proposal into the `Failed` state after 5 minutes, even if the underlying canister upgrade succeeded.
- Prevent `deployed_version` from being updated to the new version, leaving governance with a stale view of the SNS state.
- Block subsequent upgrade proposals: `check_no_upgrades_in_progress` gates new upgrades on `pending_version` being `None`, but the stale `deployed_version` causes future `get_upgrade_params` calls to compute wrong upgrade paths. [5](#0-4) 

The SNS upgrade pipeline is effectively DoS-able by any unprivileged principal for the cost of a single ingress message per upgrade attempt.

### Likelihood Explanation

The attack window opens 5 minutes after every adopted SNS upgrade proposal. The attacker only needs to monitor the public `get_running_sns_version` query to observe `pending_version.mark_failed_at_seconds`, then submit a single ingress call. No special privileges, tokens, or neurons are required. The attack is cheap, repeatable, and deterministic. [6](#0-5) 

### Recommendation

Add a caller check to `fail_stuck_upgrade_in_progress` in `rs/sns/governance/canister/canister.rs`. The function should only be callable by the SNS governance canister itself (i.e., `canister_self()`), or by a neuron holder via a governance proposal, consistent with how other privileged recovery operations are gated. At minimum, restrict it to the SNS root canister or governance canister controllers.

### Proof of Concept

1. An SNS upgrade proposal is adopted; governance sets:
   ```
   pending_version = PendingVersion {
       mark_failed_at_seconds: now + 300,
       proposal_id: Some(42),
       ...
   }
   ```
2. The actual canister upgrade completes successfully within 5 minutes, but the periodic task has not yet run to confirm it.
3. Attacker polls `get_running_sns_version` and observes `mark_failed_at_seconds` has passed.
4. Attacker sends ingress: `fail_stuck_upgrade_in_progress({})` to the SNS governance canister.
5. `now > mark_failed_at_seconds` → `complete_sns_upgrade_to_next_version` is called → `pending_version = None`, proposal 42 is marked `ExternalFailure`.
6. `deployed_version` is never updated to the new version. Future `UpgradeSnsToNextVersion` proposals compute upgrade paths from the stale `deployed_version`, causing them to attempt re-upgrading already-upgraded canisters or fail validation. [7](#0-6)

### Citations

**File:** rs/sns/governance/canister/canister.rs (L503-524)
```rust
#[query]
fn get_running_sns_version(_: GetRunningSnsVersionRequest) -> GetRunningSnsVersionResponse {
    log!(INFO, "get_running_sns_version");
    let pending_version = governance().proto.pending_version.clone();
    let upgrade_in_progress = pending_version.map(|upgrade_in_progress| UpgradeInProgress {
        target_version: upgrade_in_progress
            .target_version
            .clone()
            .map(Version::from),
        mark_failed_at_seconds: upgrade_in_progress.mark_failed_at_seconds,
        checking_upgrade_lock: upgrade_in_progress.checking_upgrade_lock,
        proposal_id: upgrade_in_progress.proposal_id.unwrap_or(0),
    });
    GetRunningSnsVersionResponse {
        deployed_version: governance()
            .proto
            .deployed_version
            .clone()
            .map(Version::from),
        pending_version: upgrade_in_progress,
    }
}
```

**File:** rs/sns/governance/canister/canister.rs (L526-535)
```rust
/// Marks an in progress upgrade that has passed its deadline as failed.
#[update]
fn fail_stuck_upgrade_in_progress(
    request: FailStuckUpgradeInProgressRequest,
) -> FailStuckUpgradeInProgressResponse {
    log!(INFO, "fail_stuck_upgrade_in_progress");
    FailStuckUpgradeInProgressResponse::from(governance_mut().fail_stuck_upgrade_in_progress(
        sns_gov_pb::FailStuckUpgradeInProgressRequest::from(request),
    ))
}
```

**File:** rs/sns/governance/src/governance.rs (L2754-2788)
```rust
    /// Used for checking that no upgrades are in progress. Also checks that there are no upgrade proposals in progress except, optionally, one that you pass in as `proposal_id`
    pub fn check_no_upgrades_in_progress(
        &self,
        proposal_id: Option<u64>,
    ) -> Result<(), GovernanceError> {
        let upgrade_proposals_in_progress = self.upgrade_proposals_in_progress();
        if !upgrade_proposals_in_progress.is_subset(&proposal_id.into_iter().collect()) {
            return Err(GovernanceError::new_with_message(
                ErrorType::ResourceExhausted,
                format!(
                    "Another upgrade is currently in progress (proposal IDs {}). \
                    Please, try again later.",
                    upgrade_proposals_in_progress
                        .into_iter()
                        .map(|id| id.to_string())
                        .collect::<Vec<String>>()
                        .join(", ")
                ),
            ));
        }

        if self.proto.pending_version.is_some() {
            return Err(GovernanceError::new_with_message(
                ErrorType::ResourceExhausted,
                format!(
                    "Upgrade lock acquired (expires at {:?}), not upgrading",
                    self.proto
                        .pending_version
                        .as_ref()
                        .map(|p| p.mark_failed_at_seconds)
                ),
            ));
        }

        Ok(())
```

**File:** rs/sns/governance/src/governance.rs (L2894-2899)
```rust
        self.proto.pending_version = Some(PendingVersion {
            target_version: Some(next_version),
            mark_failed_at_seconds: self.env.now() + 5 * 60,
            checking_upgrade_lock: 0,
            proposal_id: Some(proposal_id),
        });
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

**File:** rs/sns/governance/src/governance.rs (L6327-6361)
```rust
    /// Fails an upgrade proposal that was Adopted but not Executed or Failed by the deadline.
    pub fn fail_stuck_upgrade_in_progress(
        &mut self,
        _: FailStuckUpgradeInProgressRequest,
    ) -> FailStuckUpgradeInProgressResponse {
        let pending_version = match self.proto.pending_version.as_ref() {
            None => return FailStuckUpgradeInProgressResponse {},
            Some(pending_version) => pending_version,
        };

        // Maybe, we should look at the checking_upgrade_lock field and only
        // proceed if it is false, or the request has force set to true.

        let now = self.env.now();

        if now > pending_version.mark_failed_at_seconds {
            let message = format!(
                "Upgrade marked as failed at {}. \
                Governance upgrade was manually aborted by calling fail_stuck_upgrade_in_progress \
                after mark_failed_at_seconds ({}). Setting upgrade to failed to unblock retry.",
                format_timestamp_for_humans(now),
                pending_version.mark_failed_at_seconds,
            );
            let status = upgrade_journal_entry::upgrade_outcome::Status::ExternalFailure(Empty {});

            self.complete_sns_upgrade_to_next_version(
                pending_version.proposal_id,
                status,
                message,
                None,
            );
        }

        FailStuckUpgradeInProgressResponse {}
    }
```
