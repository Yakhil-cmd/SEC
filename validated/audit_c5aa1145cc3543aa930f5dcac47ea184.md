Audit Report

## Title
Unprivileged Caller Can Prematurely Fail SNS Upgrade Proposals via `fail_stuck_upgrade_in_progress` - (File: rs/sns/governance/canister/canister.rs)

## Summary
The `fail_stuck_upgrade_in_progress` update method on the SNS governance canister is exposed publicly with no caller access control. Any unprivileged ingress sender can invoke it after the 5-minute `mark_failed_at_seconds` deadline to forcibly mark an in-progress SNS upgrade proposal as failed and clear `pending_version` without recording the new deployed version, even if the upgrade completed successfully but the periodic confirmation task has not yet run. This creates a persistent state divergence between the actual running canister WASM and the governance canister's recorded `deployed_version`.

## Finding Description
The canister-level handler at `rs/sns/governance/canister/canister.rs` lines 526–535 carries no caller check — no `assert_eq_governance_canister_id`, no controller assertion, nothing:

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

The internal implementation at `rs/sns/governance/src/governance.rs` lines 6342–6357 fires unconditionally once `now > pending_version.mark_failed_at_seconds`, calling `complete_sns_upgrade_to_next_version` with `deployed_version: None`: [2](#0-1) 

`complete_sns_upgrade_to_next_version` at lines 6309–6313 clears `pending_version` and only updates `deployed_version` if a `Some` value is passed — which it is not in this path: [3](#0-2) 

The `mark_failed_at_seconds` deadline is set to exactly 5 minutes after upgrade initiation: [4](#0-3) 

The periodic task that normally confirms upgrade completion and clears `pending_version` runs every 10 seconds (`RUN_PERIODIC_TASKS_INTERVAL = Duration::from_secs(10)`): [5](#0-4) 

**Race window**: If an upgrade completes in the final seconds before the deadline but the periodic `check_upgrade_status` task has not yet run to confirm it, an attacker calling `fail_stuck_upgrade_in_progress` at that moment will: (1) mark the associated upgrade proposal as failed, (2) clear `pending_version` without recording the new deployed version, and (3) leave governance state permanently inconsistent with the actual running canister WASM. The window is at most one periodic task interval (~10 seconds) wide, but it is deterministically reachable with no special privileges.

## Impact Explanation
This is a **High** severity SNS governance impact. The concrete harms are: the upgrade proposal is permanently marked failed with no automated recourse; `deployed_version` is not advanced even though the canister on-chain is running the new WASM; and subsequent automatic upgrade logic (`initiate_upgrade_if_sns_behind_target_version`) will use the stale `deployed_version` to compute upgrade paths, potentially triggering redundant or incorrect upgrade sequences. This constitutes a significant SNS protocol harm with concrete and persistent state divergence — fitting the "Significant SNS security impact with concrete user or protocol harm" High category. [6](#0-5) 

## Likelihood Explanation
The attack requires no special privileges — any principal including anonymous can send an ingress update call. The attacker only needs to observe the public `pending_version` state (readable via `get_running_sns_version`) and submit the call after `mark_failed_at_seconds` elapses. The timing window is narrow (~10 seconds) but deterministic: the attacker submits the call immediately after the 5-minute deadline and succeeds if the periodic task has not yet run. The 5-minute deadline is short and predictable, making reliable timing feasible. Likelihood: **Medium-Low** — requires precise timing but zero privilege. [7](#0-6) 

## Recommendation
Add a caller access control check at the canister level in `rs/sns/governance/canister/canister.rs`, restricting `fail_stuck_upgrade_in_progress` to canister controllers or the SNS governance canister itself, analogous to how `assert_eq_governance_canister_id` is enforced in `rs/sns/root/canister/canister.rs`:

```rust
fn fail_stuck_upgrade_in_progress(...) {
    let caller = ic_cdk::api::caller();
    assert!(
        ic_cdk::api::is_controller(&caller),
        "Caller is not a controller"
    );
    // ...
}
```

Alternatively, restrict to the governance canister's own principal (self-call only), consistent with how other sensitive SNS root methods enforce caller identity. [1](#0-0) 

## Proof of Concept
1. Submit an `UpgradeSnsToNextVersion` proposal to an SNS and vote it through.
2. Observe `pending_version.mark_failed_at_seconds` via `get_running_sns_version` (public query).
3. Wait until `now > mark_failed_at_seconds` (5 minutes after upgrade start).
4. From any principal (including anonymous), immediately send an ingress update call: `fail_stuck_upgrade_in_progress({})`.
5. If the upgrade completed but `check_upgrade_status` has not yet run (window: up to 10 seconds), the proposal is marked failed, `pending_version` is cleared, and `deployed_version` is not updated.
6. Verify via `get_running_sns_version` that `deployed_version` still reflects the old version while the canister is running the new WASM.
7. A deterministic PocketIC integration test can reproduce this by: initiating an upgrade, advancing time past `mark_failed_at_seconds`, calling `fail_stuck_upgrade_in_progress` before the next periodic task tick, and asserting the resulting state divergence. [8](#0-7)

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
