Audit Report

## Title
Unauthenticated `fail_stuck_upgrade_in_progress` Endpoint Allows Any Caller to Corrupt SNS Upgrade State - (File: rs/sns/governance/canister/canister.rs)

## Summary
The SNS governance canister exposes `fail_stuck_upgrade_in_progress` as a public `#[update]` endpoint with no caller authentication. Any unprivileged ingress sender can invoke it after the 5-minute `mark_failed_at_seconds` deadline to forcibly mark an in-progress upgrade proposal as failed, leaving `deployed_version` stale and diverged from the actual running canister version, corrupting future upgrade path calculations.

## Finding Description
`perform_upgrade_to_next_sns_version_legacy` kicks off the canister upgrade and sets `pending_version` with a 5-minute deadline: [1](#0-0) 

The periodic task `check_upgrade_status` is the intended path to confirm success, calling `complete_sns_upgrade_to_next_version` with `Some(target_version)` on success: [2](#0-1) 

The public endpoint `fail_stuck_upgrade_in_progress` in the canister interface has no `caller()` check whatsoever: [3](#0-2) 

The governance-layer implementation also takes no caller parameter (`_: FailStuckUpgradeInProgressRequest`) and only checks the time deadline: [4](#0-3) 

When triggered, it calls `complete_sns_upgrade_to_next_version` with `deployed_version = None`, so `deployed_version` is never updated: [5](#0-4) 

The existing unit test `test_fails_proposal_and_removes_upgrade_if_upgrade_attempt_is_expired` explicitly confirms this behavior — `deployed_version` stays at `SNS_VERSION_1` after the call, even though the target was `SNS_VERSION_2`: [6](#0-5) 

The race condition exists because `check_upgrade_status` can be delayed: if `checking_upgrade_lock > 1` (concurrent timer firings), it returns early without confirming success: [7](#0-6) 

## Impact Explanation
This is a **High** severity SNS governance impact. An attacker who calls the endpoint after the 5-minute deadline (but before `check_upgrade_status` confirms success) causes: (1) the upgrade proposal is permanently marked failed even though the canister wasm was already installed; (2) `deployed_version` diverges from the actual running version; (3) future `UpgradeSnsToNextVersion` proposals compute the "next version" from the stale `deployed_version`, potentially re-applying the same upgrade step or skipping versions on the blessed upgrade path; (4) the `pending_version` lock is cleared, allowing a new upgrade proposal to execute immediately against the wrong baseline. This matches the allowed impact class: "Significant SNS security impact with concrete user or protocol harm."

## Likelihood Explanation
The attack requires no special privileges — any ingress sender can call the endpoint. The attack window opens exactly 5 minutes after an upgrade proposal executes and closes when `check_upgrade_status` confirms success. Under normal conditions this window is narrow, but it is deterministic and observable: an attacker can monitor on-chain state for `pending_version` and call precisely after `mark_failed_at_seconds`. The window is extended if `check_upgrade_status` is delayed by concurrent timer pressure (`checking_upgrade_lock > 1`) or transient root canister query failures.

## Recommendation
Add caller authentication to `fail_stuck_upgrade_in_progress` in `canister.rs`. Only privileged principals (e.g., SNS root canister, or neurons via governance proposal) should be permitted to abort an in-progress upgrade. The governance-layer function should accept and validate a `caller: PrincipalId` parameter. Alternatively, before marking the proposal failed, query the root canister for the actual running version and use it to update `deployed_version`, preventing state divergence even if the abort path is triggered.

## Proof of Concept
1. An `UpgradeSnsToNextVersion` proposal passes; `perform_upgrade_to_next_sns_version_legacy` sets `pending_version` with `mark_failed_at_seconds = now + 300`.
2. The actual canister upgrade completes on-chain within seconds, but `check_upgrade_status` has not yet confirmed success (e.g., concurrent timer calls set `checking_upgrade_lock > 1`).
3. After 300 seconds, an unprivileged attacker sends an ingress call to `fail_stuck_upgrade_in_progress({})`.
4. The function finds `now > mark_failed_at_seconds`, calls `complete_sns_upgrade_to_next_version(proposal_id, ExternalFailure, message, None)`.
5. The proposal is marked failed; `deployed_version` stays at the pre-upgrade value; `pending_version` is cleared.
6. The SNS governance now believes the upgrade failed while the actual running canister is at the new version — a persistent state inconsistency that corrupts future upgrade path calculations.

A deterministic unit test can reproduce this by: setting `env.now = mark_failed_at_seconds + 1`, calling `governance.fail_stuck_upgrade_in_progress(...)` without any caller check, and asserting `deployed_version == SNS_VERSION_1` (old) while the canister is actually running `SNS_VERSION_2`. The existing test `test_fails_proposal_and_removes_upgrade_if_upgrade_attempt_is_expired` already demonstrates this exact outcome. [8](#0-7)

### Citations

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

**File:** rs/sns/governance/src/governance.rs (L6261-6266)
```rust
        self.complete_sns_upgrade_to_next_version(
            proposal_id,
            status,
            message,
            Some(target_version),
        );
```

**File:** rs/sns/governance/src/governance.rs (L6309-6313)
```rust
        self.proto.pending_version = None;

        if let Some(deployed_version) = deployed_version {
            self.proto.deployed_version.replace(deployed_version);
        }
```

**File:** rs/sns/governance/src/governance.rs (L6328-6358)
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

**File:** rs/sns/governance/src/governance/fail_stuck_upgrade_in_progress_tests.rs (L216-298)
```rust
#[test]
fn test_fails_proposal_and_removes_upgrade_if_upgrade_attempt_is_expired() {
    // Step 1: Prepare the world

    let env = {
        let mut env = NativeEnvironment::new(Some(*TEST_GOVERNANCE_CANISTER_ID));

        // Note that NativeEnvironment only advances time when you tell it
        // to. Therefore, this is the time that Governance will see
        // throughout this test.
        env.now = UPGRADE_DEADLINE_TIMESTAMP_SECONDS + 1;

        env
    };

    let mut governance = Governance::new(
        ValidGovernanceProto::try_from(GOVERNANCE_PROTO.clone()).unwrap(),
        Box::new(env),
        Box::new(DoNothingLedger {}),
        Box::new(DoNothingLedger {}),
        Box::new(FakeCmc::new()),
    );

    // The code being tested is supposed to affect these fields. We
    // inspect them here to make sure that any expected changes are
    // real, not just because the world was (accidentally) already the
    // way we expected them afterwards.
    assert_eq!(
        governance.proto.pending_version.clone().unwrap(),
        PendingVersion {
            target_version: Some(SNS_VERSION_2.clone()),
            mark_failed_at_seconds: UPGRADE_DEADLINE_TIMESTAMP_SECONDS,
            checking_upgrade_lock: 10,
            proposal_id: Some(UPGRADE_PROPOSAL_ID),
        }
    );
    assert_eq!(
        governance.proto.deployed_version.clone().unwrap(),
        SNS_VERSION_1.clone()
    );

    // Step 2: Run the code being tested.
    assert_eq!(
        governance.fail_stuck_upgrade_in_progress(FailStuckUpgradeInProgressRequest {}),
        FailStuckUpgradeInProgressResponse {},
    );

    // Step 3: Inspect results.

    // Assert pending version has been cleared.
    let pending_version = &governance.proto.pending_version;
    assert!(pending_version.is_none(), "{pending_version:#?}");
    // Assert deployed_version unchanged from before.
    assert_eq!(
        governance.proto.deployed_version.clone().unwrap(),
        SNS_VERSION_1.clone()
    );

    // Assert proposal failed
    let proposal = governance.get_proposal(&GetProposal {
        proposal_id: Some(ProposalId {
            id: UPGRADE_PROPOSAL_ID,
        }),
    });
    let proposal_data = match proposal.result.unwrap() {
        get_proposal_response::Result::Error(e) => {
            panic!("Error: {e:?}")
        }
        get_proposal_response::Result::Proposal(proposal) => proposal,
    };
    assert_ne!(proposal_data.failed_timestamp_seconds, 0);

    // Inspect the proposal's failure_reason.
    let governance_error = proposal_data.failure_reason.unwrap();
    assert_eq!(
        ErrorType::try_from(governance_error.error_type),
        Ok(ErrorType::External),
        "{governance_error:#?}",
    );
    assert!(
        governance_error.error_message.contains("manually aborted"),
        "{governance_error:#?}",
    );
```
