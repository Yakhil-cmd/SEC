### Title
Unauthenticated Caller Can Abort Any In-Progress SNS Upgrade via `fail_stuck_upgrade_in_progress` - (File: rs/sns/governance/canister/canister.rs)

---

### Summary

The SNS governance canister exposes `fail_stuck_upgrade_in_progress` as a public `#[update]` method with no caller authorization check. Any unprivileged ingress sender can invoke it after the 5-minute upgrade deadline to forcibly abort an adopted-but-executing SNS upgrade proposal, marking it as `Failed` and clearing the `pending_version` lock. This is the direct IC analog of the Timelock/Governor pattern where a sensitive governance abort action is callable by the wrong party.

---

### Finding Description

`fail_stuck_upgrade_in_progress` is registered as a public canister update method:

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

There is no `caller()` check at the canister entry point. The underlying governance implementation also performs no authorization check:

```rust
pub fn fail_stuck_upgrade_in_progress(
    &mut self,
    _: FailStuckUpgradeInProgressRequest,
) -> FailStuckUpgradeInProgressResponse {
    let pending_version = match self.proto.pending_version.as_ref() {
        None => return FailStuckUpgradeInProgressResponse {},
        Some(pending_version) => pending_version,
    };
    let now = self.env.now();
    if now > pending_version.mark_failed_at_seconds {
        // ... aborts upgrade, marks proposal Failed, clears pending_version
        self.complete_sns_upgrade_to_next_version(
            pending_version.proposal_id,
            status,
            message,
            None,
        );
    }
    FailStuckUpgradeInProgressResponse {}
}
``` [2](#0-1) 

When an `UpgradeSnsToNextVersion` or `AdvanceSnsTargetVersion` proposal is adopted and execution begins, `pending_version` is set with `mark_failed_at_seconds = now + 5 * 60` (5 minutes): [3](#0-2) 

After those 5 minutes elapse, any anonymous ingress sender can call `fail_stuck_upgrade_in_progress` to:
1. Clear `proto.pending_version` (the upgrade lock)
2. Set the associated proposal's status to `Failed` with `ErrorType::External`
3. Record an `ExternalFailure` entry in the upgrade journal [4](#0-3) 

The `PendingVersion` struct and its `proposal_id` field confirm that a specific adopted governance proposal is tied to this state: [5](#0-4) 

---

### Impact Explanation

An unprivileged attacker who monitors SNS governance state can call `fail_stuck_upgrade_in_progress` immediately after the 5-minute deadline on every SNS upgrade attempt. Each call:

- Marks the adopted upgrade proposal as `Failed` (irreversible for that proposal ID)
- Clears `pending_version`, which unblocks future proposals but forces the SNS community to re-submit and re-vote on a new upgrade proposal
- Produces a misleading `ExternalFailure` journal entry, obscuring the true cause

An attacker can repeat this indefinitely, permanently preventing any SNS from upgrading its canisters as long as they monitor and call the function after each 5-minute window. This is a governance liveness attack: adopted proposals are systematically aborted by an unauthorized third party.

---

### Likelihood Explanation

The function is unconditionally reachable via any ingress message to the SNS governance canister. No stake, neuron, or privileged role is required. The 5-minute window is predictable and observable on-chain via `get_running_sns_version` or `get_upgrade_journal`. Any motivated attacker can automate this with a simple polling loop. [6](#0-5) 

---

### Recommendation

Add a caller authorization check to `fail_stuck_upgrade_in_progress`. Acceptable callers should be restricted to, for example:
- The SNS root canister
- A neuron with sufficient voting power (via a governance proposal action)
- Or at minimum, any neuron holder (requiring a valid neuron subaccount)

The comment in the implementation itself acknowledges the ambiguity:

> `// Maybe, we should look at the checking_upgrade_lock field and only proceed if it is false, or the request has force set to true.` [7](#0-6) 

This comment signals that the authorization and precondition design was left incomplete. At minimum, the canister entry point should check `caller()` against a whitelist of authorized principals before delegating to the governance logic.

---

### Proof of Concept

1. Submit an `UpgradeSnsToNextVersion` proposal to any SNS governance canister and get it adopted.
2. Observe that `pending_version` is set with `mark_failed_at_seconds = T + 300`.
3. At time `T + 301`, send an ingress update call to `fail_stuck_upgrade_in_progress` from any anonymous principal (no neuron required).
4. Observe that `pending_version` is cleared and the proposal transitions to `Failed` with `ErrorType::External` and message containing `"manually aborted"`.
5. Repeat from step 1 to permanently block all SNS upgrades. [8](#0-7)

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

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1655-1665)
```text
  message PendingVersion {
    // Version to be upgraded to
    Version target_version = 1;
    // Seconds since UNIX epoch to mark this as a failed version if not in sync with current version
    uint64 mark_failed_at_seconds = 2;
    // Lock to avoid checking over and over again. Also, it is a counter for how many times we have attempted to check,
    // allowing us to fail in case we otherwise have gotten stuck.
    uint64 checking_upgrade_lock = 3;
    // The proposal that initiated this upgrade
    optional uint64 proposal_id = 4;
  }
```

**File:** rs/sns/governance/canister/governance.did (L1050-1050)
```text
  fail_stuck_upgrade_in_progress : (record {}) -> (record {});
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
