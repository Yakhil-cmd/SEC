### Title
Unprivileged Caller Can Prematurely Fail SNS Upgrade Proposals via `fail_stuck_upgrade_in_progress` - (File: rs/sns/governance/canister/canister.rs)

### Summary
The `fail_stuck_upgrade_in_progress` update method on the SNS governance canister is exposed publicly with no caller access control. Any unprivileged ingress sender can invoke it to forcibly mark an in-progress SNS upgrade proposal as failed and clear the `pending_version` state, once the 5-minute upgrade deadline has elapsed — even if the upgrade completed successfully but the periodic confirmation task has not yet run.

### Finding Description
The canister-level handler has no caller check:

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

The internal implementation acts whenever `now > pending_version.mark_failed_at_seconds`:

```rust
if now > pending_version.mark_failed_at_seconds {
    // marks proposal as failed, clears pending_version
    self.complete_sns_upgrade_to_next_version(
        pending_version.proposal_id,
        status,
        message,
        None,
    );
}
``` [2](#0-1) 

The `mark_failed_at_seconds` deadline is set to only **5 minutes** after an upgrade is initiated:

```rust
self.proto.pending_version = Some(PendingVersion {
    target_version: Some(next_version.clone()),
    mark_failed_at_seconds: self.env.now() + 5 * 60,
    checking_upgrade_lock: 0,
    proposal_id: None,
});
``` [3](#0-2) 

The periodic task that normally confirms upgrade completion runs every 10 seconds: [4](#0-3) 

There is a race window: if an upgrade completes in the final seconds before the deadline but the periodic `check_upgrade_status` task has not yet run to confirm it and clear `pending_version`, an attacker calling `fail_stuck_upgrade_in_progress` at that moment will:
1. Mark the associated upgrade proposal as **failed** (incorrect — the upgrade succeeded)
2. Clear `pending_version` without recording the new deployed version
3. Leave governance state inconsistent with the actual running canister version

The `complete_sns_upgrade_to_next_version` call with `deployed_version: None` means the governance canister's `deployed_version` is **not updated**, even though the canister on-chain is now running the new WASM: [5](#0-4) 

### Impact Explanation
An attacker who monitors the SNS governance canister for a `pending_version` entry can call `fail_stuck_upgrade_in_progress` immediately after `mark_failed_at_seconds` elapses. If the upgrade completed but the periodic task has not yet confirmed it, the governance canister records the proposal as failed and does not advance `deployed_version`. This creates a persistent state divergence: the SNS canister runs the new code, but governance believes the old version is deployed. Subsequent automatic upgrade logic (`initiate_upgrade_if_sns_behind_target_version`) will use the stale `deployed_version` to compute upgrade paths, potentially triggering redundant or incorrect upgrades. The associated proposal is permanently marked failed with no recourse. [6](#0-5) 

### Likelihood Explanation
The attack window is narrow (a few seconds between upgrade completion and the next periodic task run), but it is deterministically reachable by any unprivileged ingress sender with no special privileges. The attacker only needs to observe the public `pending_version` state (readable via `get_running_sns_version`) and submit a single ingress message at the right moment. The 5-minute deadline is short enough that an attacker can reliably time this. Likelihood: **Medium-Low** — requires timing but no privileged access.

### Recommendation
Add a caller access control check to `fail_stuck_upgrade_in_progress` at the canister level, restricting it to the SNS governance canister's controllers or a designated admin principal, analogous to how other sensitive SNS root methods enforce `assert_eq_governance_canister_id`: [7](#0-6) 

At minimum, restrict the function to canister controllers using `ic_cdk::api::is_controller(&ic_cdk::api::msg_caller())`.

### Proof of Concept
1. Submit an `UpgradeSnsToNextVersion` or `UpgradeSnsControlledCanister` proposal to an SNS and vote it through.
2. Observe `pending_version.mark_failed_at_seconds` via `get_running_sns_version`.
3. Wait until `now > mark_failed_at_seconds` (5 minutes after upgrade start).
4. From any principal (including anonymous), send an ingress update call to the SNS governance canister: `fail_stuck_upgrade_in_progress({})`.
5. If the upgrade completed but `check_upgrade_status` has not yet run, the proposal is marked failed and `deployed_version` is not updated, leaving governance state permanently inconsistent with the actual running canister. [1](#0-0) [8](#0-7)

### Citations

**File:** rs/sns/governance/canister/canister.rs (L76-76)
```rust
const RUN_PERIODIC_TASKS_INTERVAL: Duration = Duration::from_secs(10);
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

**File:** rs/sns/governance/src/governance.rs (L5636-5641)
```rust
        self.proto.pending_version = Some(PendingVersion {
            target_version: Some(next_version.clone()),
            mark_failed_at_seconds: self.env.now() + 5 * 60,
            checking_upgrade_lock: 0,
            proposal_id: None,
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

**File:** rs/sns/root/canister/canister.rs (L419-426)
```rust
fn assert_eq_governance_canister_id(id: PrincipalId) {
    STATE.with(|state: &RefCell<SnsRootCanister>| {
        let state = state.borrow();
        let governance_canister_id = state
            .governance_canister_id
            .expect("STATE.governance_canister_id is not populated");
        assert_eq!(id, governance_canister_id);
    });
```
