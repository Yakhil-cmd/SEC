### Title
Unprivileged Caller Can Abort a Legitimately In-Progress SNS Upgrade via `fail_stuck_upgrade_in_progress` - (File: rs/sns/governance/canister/canister.rs)

### Summary
The SNS governance upgrade state machine exposes `fail_stuck_upgrade_in_progress` as a public, unauthenticated `#[update]` endpoint. Any unprivileged ingress sender can call it after the 5-minute `mark_failed_at_seconds` deadline to forcibly mark a successfully-executed upgrade proposal as **failed** and leave `deployed_version` stale, creating a persistent governance state inconsistency.

### Finding Description
When an `UpgradeSnsToNextVersion` proposal is adopted and executed, `perform_upgrade_to_next_sns_version_legacy` kicks off the actual canister upgrade and then sets `pending_version` with a 5-minute deadline: [1](#0-0) 

The periodic task `check_upgrade_status` is supposed to detect completion and call `complete_sns_upgrade_to_next_version` with `Status::Success`. However, the public endpoint `fail_stuck_upgrade_in_progress` performs the same state transition — clearing `pending_version` and marking the proposal failed — with **no caller authentication**: [2](#0-1) 

The implementation only checks whether the current time exceeds `mark_failed_at_seconds`: [3](#0-2) 

When triggered, `complete_sns_upgrade_to_next_version` is called with `deployed_version = None`, so `deployed_version` is **not** updated to the new version, and the proposal is permanently marked as failed: [4](#0-3) 

This is the direct analog to the `ProtocolUpgradeHandler` design flaw: two paths (`check_upgrade_status` and `fail_stuck_upgrade_in_progress`) operate on the same `pending_version` state with conflicting outcomes, and the "abort" path is accessible to any unprivileged actor after a fixed deadline — creating a race condition where a successful upgrade can be recorded as failed.

### Impact Explanation
An attacker who calls `fail_stuck_upgrade_in_progress` after the 5-minute deadline (but before `check_upgrade_status` confirms success) causes:

1. The upgrade proposal is permanently marked as **failed** even though the actual canister wasm was already installed.
2. `deployed_version` remains at the pre-upgrade value, diverging from the actual running version.
3. Future `UpgradeSnsToNextVersion` proposals compute the "next version" from the stale `deployed_version`, potentially re-applying the same upgrade step or skipping versions on the blessed upgrade path.
4. The `check_no_upgrades_in_progress` lock is cleared, allowing a new upgrade proposal to execute immediately against the wrong baseline version. [5](#0-4) 

### Likelihood Explanation
The attack window is narrow (the upgrade must still be in `pending_version` state after 5 minutes), but it is reachable by any unprivileged ingress sender with no special privileges. The `checking_upgrade_lock` mechanism can delay `check_upgrade_status` from completing within the window: [6](#0-5) 

If `check_upgrade_status` is called more than once before the async call returns (e.g., under timer pressure), `checking_upgrade_lock > 1` causes it to return early, extending the window. An attacker monitoring on-chain state can observe `pending_version` and call `fail_stuck_upgrade_in_progress` precisely after the deadline.

### Recommendation
Add caller authentication to `fail_stuck_upgrade_in_progress`. Only privileged principals (e.g., SNS governance neurons via proposal, or the SNS root canister) should be permitted to abort an in-progress upgrade. Alternatively, redesign the state machine so that the abort path records the actual running version (via a root canister query) before marking the proposal failed, preventing `deployed_version` from becoming stale.

### Proof of Concept
1. An `UpgradeSnsToNextVersion` proposal passes and `perform_upgrade_to_next_sns_version_legacy` sets `pending_version` with `mark_failed_at_seconds = now + 300`.
2. The actual canister upgrade completes on-chain within seconds, but `check_upgrade_status` has not yet run (e.g., `checking_upgrade_lock > 1` from a concurrent timer call).
3. After 300 seconds, an unprivileged attacker sends an ingress call to `fail_stuck_upgrade_in_progress({})`.
4. `fail_stuck_upgrade_in_progress` finds `now > mark_failed_at_seconds`, calls `complete_sns_upgrade_to_next_version` with `deployed_version = None` and `Status::ExternalFailure`.
5. The proposal is marked failed; `deployed_version` stays at the old version; `pending_version` is cleared.
6. The SNS governance now believes the upgrade failed, while the actual running canister is at the new version — a persistent state inconsistency that corrupts future upgrade path calculations. [7](#0-6) [2](#0-1)

### Citations

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

**File:** rs/sns/governance/src/governance.rs (L6169-6171)
```rust
        if lock > 1 {
            return;
        }
```

**File:** rs/sns/governance/src/governance.rs (L6305-6313)
```rust
        if let Some(proposal_id) = proposal_id {
            self.set_proposal_execution_status(proposal_id, result);
        }

        self.proto.pending_version = None;

        if let Some(deployed_version) = deployed_version {
            self.proto.deployed_version.replace(deployed_version);
        }
```

**File:** rs/sns/governance/src/governance.rs (L6328-6361)
```rust
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
