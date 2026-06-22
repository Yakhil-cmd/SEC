### Title
`fail_stuck_upgrade_in_progress` Callable by Any Unprivileged User Without Authorization Check - (`File: rs/sns/governance/canister/canister.rs`)

### Summary
The `fail_stuck_upgrade_in_progress` endpoint on the SNS Governance canister is exposed as a public `#[update]` method with no caller authorization check. Any unprivileged ingress sender can invoke it to forcibly mark an in-progress SNS upgrade proposal as failed (once the `mark_failed_at_seconds` deadline has elapsed), bypassing the expectation that only governance participants or authorized parties should be able to intervene in the upgrade lifecycle.

### Finding Description

The canister-level handler in `rs/sns/governance/canister/canister.rs` exposes `fail_stuck_upgrade_in_progress` with no caller restriction:

```rust
#[update]
fn fail_stuck_upgrade_in_progress(
    request: FailStuckUpgradeInProgressRequest,
) -> FailStuckUpgradeInProgressResponse {
    log!(INFO, "fail_stuck_upgrade_in_progress");
    FailStuckUpgradeInProgressResponse::from(governance_mut().fail_stuck_upgrade_in_progress(
        sns_gov_pb::FailStuckUpgradeInProgressRequest::from(request),
    ))
}
``` [1](#0-0) 

The underlying implementation in `rs/sns/governance/src/governance.rs` only checks a time condition (`now > pending_version.mark_failed_at_seconds`) before clearing `pending_version` and marking the associated upgrade proposal as failed:

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
        ...
        self.complete_sns_upgrade_to_next_version(
            pending_version.proposal_id, status, message, None,
        );
    }
    FailStuckUpgradeInProgressResponse {}
}
``` [2](#0-1) 

The function is documented as a recovery mechanism for cases where the asynchronous upgrade process has failed to complete: [3](#0-2) 

Critically, the code itself contains a developer note acknowledging the missing guard: `"Maybe, we should look at the checking_upgrade_lock field and only proceed if it is false, or the request has force set to true."` This confirms the intent was to add additional restrictions that were never implemented. [4](#0-3) 

The `checking_upgrade_lock` field is used by the periodic task `check_upgrade_status` to prevent concurrent checks. However, `fail_stuck_upgrade_in_progress` ignores this lock entirely, meaning an external caller can race against the periodic task and force-fail an upgrade even while the periodic task is actively verifying the upgrade status. [5](#0-4) 

### Impact Explanation

Any unprivileged ingress sender can:

1. Wait for an SNS upgrade proposal's `mark_failed_at_seconds` deadline to elapse (which happens when the upgrade takes longer than expected, e.g., 5 minutes).
2. Call `fail_stuck_upgrade_in_progress` to immediately clear `pending_version` and mark the upgrade proposal as `Failed`.
3. This unblocks the submission of new upgrade proposals, allowing the attacker to race to submit a malicious `UpgradeSnsToNextVersion` proposal before legitimate governance participants can retry.
4. Additionally, by ignoring `checking_upgrade_lock`, the call can race against the periodic task's async inter-canister call to `get_running_version`, potentially causing inconsistent state.

The function `complete_sns_upgrade_to_next_version` sets `self.proto.pending_version = None` and calls `set_proposal_execution_status` with a failure result, permanently marking the proposal as failed with no recourse. [6](#0-5) 

### Likelihood Explanation

Medium. The attacker must wait for an upgrade deadline to elapse, which requires an upgrade to be in progress and to have exceeded its 5-minute window. This is a realistic scenario during network congestion or canister unavailability. The attack requires no special privileges — any principal can send an ingress message to the SNS Governance canister.

### Recommendation

Add a caller authorization check to `fail_stuck_upgrade_in_progress`. The function should only be callable by:
- SNS neuron holders (via a governance proposal), or
- A designated privileged principal (e.g., the SNS root canister or a specific hot key).

At minimum, the `checking_upgrade_lock` guard mentioned in the developer comment should be enforced: if `checking_upgrade_lock > 0`, the function should refuse to act (or require an explicit `force: true` flag from an authorized caller).

### Proof of Concept

1. An SNS has an `UpgradeSnsToNextVersion` proposal adopted and executing, with `pending_version.mark_failed_at_seconds = T`.
2. The upgrade takes longer than expected; time advances past `T`.
3. Attacker sends an ingress message to the SNS Governance canister calling `fail_stuck_upgrade_in_progress({})`.
4. The call succeeds: `pending_version` is cleared, the upgrade proposal is marked `Failed`.
5. The attacker can now submit a new `UpgradeSnsToNextVersion` proposal (since `check_no_upgrades_in_progress` now passes) before legitimate participants notice. [1](#0-0) [7](#0-6)

### Citations

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

**File:** rs/sns/governance/src/governance.rs (L6145-6171)
```rust
        // Mark the check as active before async call.
        self.proto
            .pending_version
            .as_mut()
            .unwrap()
            .checking_upgrade_lock += 1;

        let lock = self
            .proto
            .pending_version
            .as_ref()
            .unwrap()
            .checking_upgrade_lock;

        if lock > 1000 {
            let message =
                "Too many attempts to check upgrade without success.  Marking upgrade failed."
                    .to_string();
            let status = upgrade_journal_entry::upgrade_outcome::Status::Timeout(Empty {});

            self.complete_sns_upgrade_to_next_version(proposal_id, status, message, None);
            return;
        }

        if lock > 1 {
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

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L2327-2341)
```rust
    pub struct PendingVersion {
        /// Version to be upgraded to
        #[prost(message, optional, tag = "1")]
        pub target_version: ::core::option::Option<Version>,
        /// Seconds since UNIX epoch to mark this as a failed version if not in sync with current version
        #[prost(uint64, tag = "2")]
        pub mark_failed_at_seconds: u64,
        /// Lock to avoid checking over and over again. Also, it is a counter for how many times we have attempted to check,
        /// allowing us to fail in case we otherwise have gotten stuck.
        #[prost(uint64, tag = "3")]
        pub checking_upgrade_lock: u64,
        /// The proposal that initiated this upgrade
        #[prost(uint64, optional, tag = "4")]
        pub proposal_id: ::core::option::Option<u64>,
    }
```
