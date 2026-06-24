### Title
SNS Governance Canister Panic via Race Between `check_upgrade_status` and `fail_stuck_upgrade_in_progress` - (File: `rs/sns/governance/src/governance.rs`)

### Summary
The SNS governance canister contains a race condition between the periodic `check_upgrade_status` task and the publicly callable `fail_stuck_upgrade_in_progress` method. Both paths converge on `set_proposal_execution_status`, which contains a hard `assert_eq!` requiring the proposal to be in `Adopted` state. If `fail_stuck_upgrade_in_progress` is called while `check_upgrade_status` is suspended at an async await point, both paths will call `set_proposal_execution_status` on the same proposal, causing the second call to panic the canister and corrupt governance state.

### Finding Description

**Root cause — hard assert with no double-call guard in SNS `set_proposal_execution_status`:** [1](#0-0) 

Unlike the NNS counterpart (which uses `debug_assert_eq!` and has an explicit `executed_timestamp_seconds != 0` early-return guard), the SNS version uses a hard `assert_eq!` at line 1720 and has no guard against being called twice. If the proposal is already in `Failed` or `Executed` state when this function is called, the canister traps.

**Two independent callers converge on the same proposal:**

Path 1 — `check_upgrade_status` (periodic heartbeat task): [2](#0-1) 

Path 2 — `fail_stuck_upgrade_in_progress` (public, no authorization check, ignores `checking_upgrade_lock`): [3](#0-2) 

Both paths call `complete_sns_upgrade_to_next_version`, which calls `set_proposal_execution_status` before clearing `pending_version`: [4](#0-3) 

**The `checking_upgrade_lock` does not protect against this race.** The comment in `fail_stuck_upgrade_in_progress` explicitly acknowledges this gap:

> "Maybe, we should look at the checking_upgrade_lock field and only proceed if it is false, or the request has force set to true."

**Race sequence:**
1. `check_upgrade_status` starts, increments `checking_upgrade_lock` to 1, clones `pending_version` as local `upgrade_in_progress` (containing `proposal_id = X`), then suspends at an async inter-canister call (e.g., `get_canister_status`).
2. During the await, an unprivileged user calls `fail_stuck_upgrade_in_progress`. The deadline (`mark_failed_at_seconds = now + 5*60`) has elapsed. The function calls `complete_sns_upgrade_to_next_version(Some(X), Err(...))` → `set_proposal_execution_status(X, Err(...))`. Proposal X transitions from `Adopted` → `Failed`. `pending_version` is set to `None`.
3. `check_upgrade_status` resumes. It uses its local `upgrade_in_progress` (still holding `proposal_id = X`). The upgrade is confirmed complete; it calls `complete_sns_upgrade_to_next_version(Some(X), Ok(()))` → `set_proposal_execution_status(X, Ok(()))`.
4. The hard `assert_eq!(proposal.status(), ProposalDecisionStatus::Adopted)` fires — proposal X is now `Failed`, not `Adopted` — **the canister traps**.

### Impact Explanation

- The SNS governance canister heartbeat traps, rolling back state changes from that message. However, the state mutation from step 2 (`fail_stuck_upgrade_in_progress`) is already committed in a prior message and is not rolled back.
- Net persistent state: proposal X is permanently marked `Failed` even though the underlying canister upgrade succeeded. `pending_version` is `None`.
- The SNS governance canister now has an inconsistent view of its own version: the actual deployed canister runs the new WASM, but governance believes the upgrade failed. This can block or corrupt subsequent upgrade proposals and version tracking.
- Repeated exploitation can be used to permanently desynchronize the SNS governance version state from the actual deployed state.

### Likelihood Explanation

- `fail_stuck_upgrade_in_progress` is a public update call with no caller authorization check.
- The 5-minute deadline (`mark_failed_at_seconds`) is reachable whenever an upgrade takes longer than expected (e.g., slow subnet, large WASM).
- `check_upgrade_status` is called on every heartbeat and makes at least one async inter-canister call, creating a recurring window of several seconds per heartbeat cycle.
- An attacker can spam `fail_stuck_upgrade_in_progress` calls at high frequency after the deadline, making it highly probable to land within the async window.
- No special privileges, keys, or majority corruption are required.

### Recommendation

1. **Add a re-entrancy guard in `set_proposal_execution_status` (SNS):** Before the assert, check if the proposal is already in a terminal state (`Executed` or `Failed`) and return early (log a warning), mirroring the NNS behavior at line 3205.
2. **Respect `checking_upgrade_lock` in `fail_stuck_upgrade_in_progress`:** Implement the TODO noted in the comment — refuse to proceed if `checking_upgrade_lock > 0`.
3. **Re-check `pending_version` after async suspension in `check_upgrade_status`:** After resuming from an await, verify that `pending_version` is still set and matches the locally cached `upgrade_in_progress` before calling `complete_sns_upgrade_to_next_version`.

### Proof of Concept

```
1. Submit an SNS UpgradeSnsToNextVersion proposal and get it adopted.
2. Wait for mark_failed_at_seconds (5 minutes) to elapse.
3. In a tight loop, send update calls to sns_governance.fail_stuck_upgrade_in_progress({}).
4. Observe: one call lands while check_upgrade_status is suspended at its async get_canister_status call.
5. Result: proposal is marked Failed; subsequent heartbeat traps; SNS governance version state is permanently desynchronized from actual deployed version.
``` [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** rs/sns/governance/src/governance.rs (L1716-1730)
```rust
    pub fn set_proposal_execution_status(&mut self, pid: u64, result: Result<(), GovernanceError>) {
        match self.proto.proposals.get_mut(&pid) {
            Some(proposal) => {
                // The proposal has to be adopted before it is executed.
                assert_eq!(proposal.status(), ProposalDecisionStatus::Adopted);
                match result {
                    Ok(_) => {
                        log!(INFO, "Execution of proposal: {} succeeded.", pid);
                        // The proposal was executed 'now'.
                        proposal.executed_timestamp_seconds = self.env.now();
                        // If the proposal was executed it has not failed,
                        // thus we set the failed_timestamp_seconds to zero
                        // (it should already be zero, but let's be defensive).
                        proposal.failed_timestamp_seconds = 0;
                        proposal.failure_reason = None;
```

**File:** rs/sns/governance/src/governance.rs (L6107-6172)
```rust
    async fn check_upgrade_status(&mut self) {
        // This expect is safe because we only call this after checking exactly that condition in
        // should_check_upgrade_status
        let upgrade_in_progress = self
            .proto
            .pending_version
            .as_ref()
            .expect("There must be pending_version or should_check_upgrade_status returns false")
            .clone();

        if upgrade_in_progress.target_version.is_none() {
            // If we have an upgrade_in_progress with no target_version, we are in an unexpected
            // situation. We recover to workable state by marking upgrade as failed.

            let message = "No target_version set for upgrade_in_progress. This should be \
                impossible. Clearing upgrade_in_progress state and marking proposal failed \
                to unblock further upgrades."
                .to_string();

            let status = upgrade_journal_entry::upgrade_outcome::Status::InvalidState(
                upgrade_journal_entry::upgrade_outcome::InvalidState { version: None },
            );

            self.complete_sns_upgrade_to_next_version(
                upgrade_in_progress.proposal_id,
                status,
                message,
                None,
            );

            return;
        }

        // Pre-checks finished, we now extract needed variables.
        let target_version = upgrade_in_progress.target_version.as_ref().unwrap().clone();
        let mark_failed_at = upgrade_in_progress.mark_failed_at_seconds;
        let proposal_id = upgrade_in_progress.proposal_id;

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

**File:** rs/sns/governance/src/governance.rs (L6280-6314)
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
