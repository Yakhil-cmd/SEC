### Title
Unprotected `fail_stuck_upgrade_in_progress` Allows Anyone to Abort In-Progress SNS Upgrades and Corrupt Governance State - (`File: rs/sns/governance/canister/canister.rs`)

---

### Summary

The SNS Governance canister exposes `fail_stuck_upgrade_in_progress` as a public `#[update]` endpoint with no caller authentication. Any unprivileged ingress sender can invoke it after the 5-minute upgrade deadline to forcibly mark a governance-approved SNS canister upgrade as failed, clearing `pending_version` and setting the associated proposal's execution status to failed — even while the actual canister upgrade may still be completing on-chain.

---

### Finding Description

The `fail_stuck_upgrade_in_progress` endpoint in `rs/sns/governance/canister/canister.rs` has no access control:

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
``` [1](#0-0) 

No `caller()` check, no `inspect_message` guard, and no allowlist is applied. The underlying `fail_stuck_upgrade_in_progress` logic in `rs/sns/governance/src/governance.rs` checks only whether the current time exceeds `mark_failed_at_seconds`:

```rust
if now > pending_version.mark_failed_at_seconds {
    // ...
    self.complete_sns_upgrade_to_next_version(
        pending_version.proposal_id,
        status,
        message,
        None,
    );
}
``` [2](#0-1) 

`complete_sns_upgrade_to_next_version` irreversibly:
1. Sets the governance proposal's execution status to `Failed`
2. Clears `proto.pending_version`
3. Does **not** update `deployed_version` [3](#0-2) 

The deadline is set to only **5 minutes** after the upgrade is kicked off:

```rust
self.proto.pending_version = Some(PendingVersion {
    target_version: Some(next_version),
    mark_failed_at_seconds: self.env.now() + 5 * 60,
    ...
});
``` [4](#0-3) 

SNS canister upgrades (especially root upgrades) routinely take longer than 5 minutes due to consensus latency and state checkpointing. Once the deadline passes, any anonymous or unprivileged principal can call `fail_stuck_upgrade_in_progress` to abort the upgrade in governance state.

---

### Impact Explanation

An attacker can:

1. Monitor any SNS for an in-progress upgrade (visible via `get_running_sns_version` query).
2. Wait for `mark_failed_at_seconds` to pass (≥5 minutes after upgrade initiation).
3. Call `fail_stuck_upgrade_in_progress` as any principal (including anonymous).
4. This causes:
   - The governance proposal to be permanently marked as `Failed`.
   - `pending_version` to be cleared, unblocking future proposals — but with `deployed_version` still pointing to the old version.
   - If the actual canister WASM installation completed before the abort, the SNS canister is now running the new code while governance believes it is still on the old version. All future upgrade proposals will be computed against the wrong baseline version, leading to a persistent governance/canister state desynchronization.
   - If the installation had not yet completed, the upgrade is silently abandoned and the SNS community must re-submit a new governance proposal.

This is a **governance authorization bug** with a realistic DoS-on-governance impact: an attacker can repeatedly abort every SNS upgrade proposal after the 5-minute window, permanently blocking any SNS from upgrading its canisters.

---

### Likelihood Explanation

- The function is reachable by any ingress sender with no preconditions beyond the upgrade deadline having passed.
- The 5-minute deadline is short relative to real-world upgrade latency.
- The attack requires no funds, no tokens, no neuron, and no special role — only the ability to submit an update call to the SNS governance canister.
- The SNS governance canister is a publicly deployed system canister reachable from the IC boundary nodes.

---

### Recommendation

Add a caller authorization check to `fail_stuck_upgrade_in_progress`. Only SNS neuron holders (or a designated privileged principal such as the SNS root canister) should be permitted to invoke this function. At minimum, reject anonymous callers:

```rust
#[update]
fn fail_stuck_upgrade_in_progress(
    request: FailStuckUpgradeInProgressRequest,
) -> FailStuckUpgradeInProgressResponse {
    let caller = caller();
    // Require caller to be a known neuron holder or the root canister
    governance().require_caller_has_neuron_or_is_root(caller);
    ...
}
```

Alternatively, increase `mark_failed_at_seconds` to a value that reflects realistic upgrade durations (e.g., 30–60 minutes), and add an `inspect_message` hook to reject anonymous callers before consensus.

---

### Proof of Concept

1. Deploy or identify an SNS on mainnet with an active upgrade proposal.
2. Observe `pending_version.mark_failed_at_seconds` via `get_running_sns_version` query.
3. After the deadline passes, submit:
   ```
   dfx canister --network ic call <sns-governance-canister-id> \
     fail_stuck_upgrade_in_progress '(record {})'
   ```
   as any principal (including anonymous via `--no-wallet`).
4. Observe that the upgrade proposal is now marked `Failed` and `pending_version` is cleared, while the SNS canister may be running the new WASM.

The test at `rs/sns/governance/src/governance/fail_stuck_upgrade_in_progress_tests.rs` confirms the function mutates state when `now > mark_failed_at_seconds` with no caller check in the test setup, demonstrating the absence of access control is by design (not an oversight in tests only). [5](#0-4)

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

**File:** rs/sns/governance/src/governance.rs (L2894-2899)
```rust
        self.proto.pending_version = Some(PendingVersion {
            target_version: Some(next_version),
            mark_failed_at_seconds: self.env.now() + 5 * 60,
            checking_upgrade_lock: 0,
            proposal_id: Some(proposal_id),
        });
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

**File:** rs/sns/governance/src/governance.rs (L6342-6358)
```rust
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

**File:** rs/sns/governance/src/governance/fail_stuck_upgrade_in_progress_tests.rs (L216-299)
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
}
```
