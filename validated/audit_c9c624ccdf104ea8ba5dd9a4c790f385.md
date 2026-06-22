### Title
Unprivileged Caller Can Prematurely Abort a Legitimate SNS Upgrade via `fail_stuck_upgrade_in_progress` — (`rs/sns/governance/src/governance.rs`)

### Summary

`fail_stuck_upgrade_in_progress` is a public, unauthenticated update method on the SNS Governance canister. It accepts calls from any principal — including anonymous — and will mark an in-progress upgrade proposal as `Failed` and clear `pending_version` whenever `now > mark_failed_at_seconds`. The deadline is only **5 minutes** from when the upgrade is kicked off. Because the upgrade verification is asynchronous (a periodic task polling `get_sns_canisters_summary` via root), the deadline can elapse while the upgrade is still legitimately completing, allowing any external caller to abort it.

---

### Finding Description

**Entrypoint — no authorization check:**

The canister handler at `rs/sns/governance/canister/canister.rs` passes no `caller()` to the governance method:

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

**Governance method — only a time check, no caller or lock check:**

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
            pending_version.proposal_id,
            status,
            message,
            None,
        );
    }
    FailStuckUpgradeInProgressResponse {}
}
``` [2](#0-1) 

The comment at line 6337–6338 is a developer-acknowledged gap: the `checking_upgrade_lock` field is **not** consulted before aborting.

**The deadline is only 5 minutes:**

Both upgrade paths set `mark_failed_at_seconds` to `now + 5 * 60`: [3](#0-2) [4](#0-3) 

**Effect of `complete_sns_upgrade_to_next_version`:**

- Sets `pending_version = None` — disabling the periodic `check_upgrade_status` task
- Calls `set_proposal_execution_status(proposal_id, Err(...))` — marks the proposal `Failed`
- Does **not** update `deployed_version` — governance state is now inconsistent with the actual on-chain canister state [5](#0-4) 

---

### Impact Explanation

1. Any anonymous principal calls `fail_stuck_upgrade_in_progress({})` after the 5-minute deadline.
2. The upgrade proposal is marked `Failed`; `pending_version` is cleared.
3. `deployed_version` is **not** updated, even if the canister WASM was already installed on-chain.
4. Because `pending_version` is `None`, `should_check_upgrade_status()` returns `false` — the periodic task will never self-correct.
5. No further `UpgradeSnsToNextVersion` proposal can execute until a new one is submitted and adopted (the pipeline is blocked by the inconsistent state).

The dapp canisters may be running the new WASM while governance believes they are on the old version, or the upgrade may have been genuinely in-flight and is now abandoned.

---

### Likelihood Explanation

- The 5-minute window is short. On a loaded subnet, the periodic task (`run_periodic_tasks`) may not complete its async `get_sns_canisters_summary` round-trip within 5 minutes.
- `mark_failed_at_seconds` and `proposal_id` are publicly readable via `get_running_sns_version` (a query call), so any observer can watch for the deadline and call immediately after.
- No tokens, keys, or privileged access are required — an anonymous principal suffices.
- The `checking_upgrade_lock` field is publicly visible and could be used to confirm the periodic task is not actively checking, but the function does not require this.

---

### Recommendation

1. **Add an authorization check**: Restrict `fail_stuck_upgrade_in_progress` to governance neurons (via `manage_neuron`) or to the governance canister itself (self-call), consistent with how other privileged operations are guarded.
2. **Alternatively, check `checking_upgrade_lock`**: Only proceed if `checking_upgrade_lock == 0`, ensuring the periodic task is not mid-flight. The developer TODO at line 6337–6338 already identifies this gap.
3. **Extend the deadline**: 5 minutes is very tight for a multi-canister upgrade verification round-trip on a loaded subnet. A longer deadline (e.g., 30 minutes) reduces the attack surface.

---

### Proof of Concept

```rust
// State-machine test sketch
// 1. Adopt UpgradeSnsToNextVersion proposal → pending_version set with mark_failed_at = now + 300
// 2. Advance time by 301 seconds (deadline elapsed, upgrade may still be verifying)
// 3. Anonymous principal calls fail_stuck_upgrade_in_progress({})
// 4. Assert: pending_version == None, proposal.failed_timestamp_seconds != 0,
//            deployed_version unchanged (old version)
```

This is directly confirmed by the existing unit test `test_fails_proposal_and_removes_upgrade_if_upgrade_attempt_is_expired`, which sets `env.now = UPGRADE_DEADLINE_TIMESTAMP_SECONDS + 1` and calls `fail_stuck_upgrade_in_progress` with no caller check — the proposal is marked Failed and `pending_version` is cleared: [6](#0-5)

### Citations

**File:** rs/sns/governance/canister/canister.rs (L527-535)
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

**File:** rs/sns/governance/src/governance.rs (L5636-5641)
```rust
        self.proto.pending_version = Some(PendingVersion {
            target_version: Some(next_version.clone()),
            mark_failed_at_seconds: self.env.now() + 5 * 60,
            checking_upgrade_lock: 0,
            proposal_id: None,
        });
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

**File:** rs/sns/governance/src/governance/fail_stuck_upgrade_in_progress_tests.rs (L217-299)
```rust
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
