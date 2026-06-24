Audit Report

## Title
Incorrect Upgrade Lock Order in SNS Governance Allows Concurrent Upgrade Proposals to Bypass Guard - (File: `rs/sns/governance/src/governance.rs`)

## Summary
In `perform_upgrade_to_next_sns_version_legacy`, the `pending_version` upgrade lock is set only after all async inter-canister calls complete (L2894–2899), leaving it `None` across every `await` point. Because `check_no_upgrades_in_progress` (L2828) tests only `pending_version`, a second concurrently executing `UpgradeSnsToNextVersion` proposal can pass the guard while the first is suspended at an inter-canister call, causing two simultaneous upgrades. The newer path `initiate_upgrade_if_sns_behind_target_version` correctly sets the lock before its async call (L5636–5641), making the inconsistency the root cause.

## Finding Description
`start_proposal_execution` transmutes `self` to a `'static` reference and calls `spawn_in_canister_env`, scheduling `perform_action` as a background future in the canister's cooperative async runtime. [1](#0-0) 

When a spawned future suspends at an `await` point (inter-canister call), the IC runtime can poll other ready futures or process incoming callbacks. `perform_upgrade_to_next_sns_version_legacy` has four such suspension points before the lock is ever set: [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) 

The lock is only written after all of the above complete: [7](#0-6) 

By contrast, `initiate_upgrade_if_sns_behind_target_version` sets `pending_version` before its async call: [8](#0-7) 

A second proposal's execution can begin (in a new message/callback context) while P1 is suspended. It calls `check_no_upgrades_in_progress`, observes `pending_version == None`, and proceeds with its own upgrade. Both proposals then independently call `upgrade_non_root_canister` on the same SNS canister. When P1 finishes, it writes `pending_version = {target: V2}`; when P2 finishes, it overwrites `pending_version = {target: V3}`. `check_upgrade_status` then resolves against V3 only, marks the upgrade succeeded, sets `deployed_version = V3`, and clears `pending_version`: [9](#0-8) 

P1's `proposal_id` is lost from `pending_version`, so `set_proposal_execution_status` is never called for P1, leaving it permanently adopted-but-unresolved and skipping V2 in the upgrade journal.

## Impact Explanation
This is a **High** severity finding matching the allowed impact: *"Significant SNS security impact with concrete user or protocol harm."* Specifically:
- Two WASM installs execute on the same SNS canister simultaneously, potentially with different hashes.
- `deployed_version` is permanently set to an incorrect value, corrupting the SNS upgrade journal.
- Subsequent step-by-step upgrade proposals are blocked or incorrectly evaluated because `deployed_version` no longer reflects reality.
- One proposal is permanently left in an adopted-but-unresolved state with no recovery path short of manual intervention.

## Likelihood Explanation
Any neuron holder can submit `UpgradeSnsToNextVersion` proposals. Two such proposals reaching the voting threshold within overlapping execution windows is a realistic, non-malicious scenario when the SNS-W upgrade path has multiple pending steps. No malicious governance majority is required — normal concurrent adoption suffices. The async window spans multiple IC rounds (stop → install_code → start through root), providing a wide interleaving window.

## Recommendation
Move `self.proto.pending_version = Some(PendingVersion { ... })` to immediately after `check_no_upgrades_in_progress` and before the first `await` in `perform_upgrade_to_next_sns_version_legacy`, mirroring the pattern in `initiate_upgrade_if_sns_behind_target_version`. Add a corresponding `self.proto.pending_version = None` in all error return paths (as already done at L5650) so a failed early async call does not leave a stale lock. [10](#0-9) 

## Proof of Concept
1. Deploy an SNS with `deployed_version = V1`, `pending_version = None`.
2. Submit two `UpgradeSnsToNextVersion` proposals P1 (targeting V2) and P2 (targeting V3); both reach voting threshold.
3. `start_proposal_execution` spawns both as background futures via `spawn_in_canister_env`.
4. P1 executes first, passes `check_no_upgrades_in_progress` (`pending_version == None`), suspends at `get_upgrade_params(...).await`.
5. P2 begins execution (new message or cooperative poll), passes `check_no_upgrades_in_progress` (`pending_version` still `None`), proceeds through all awaits.
6. P2 calls `upgrade_non_root_canister` for V3; P1 resumes and calls `upgrade_non_root_canister` for V2.
7. P1 completes, sets `pending_version = {target: V2, proposal_id: P1}`.
8. P2 completes, overwrites `pending_version = {target: V3, proposal_id: P2}`.
9. `check_upgrade_status` sees running version = V3, calls `complete_sns_upgrade_to_next_version` with `proposal_id = P2` only, sets `deployed_version = V3`, clears `pending_version`.
10. P1's proposal is never marked executed; `deployed_version` skips V2; SNS upgrade journal is permanently corrupted.

A deterministic integration test using PocketIC can reproduce this by spawning both proposal futures in the same canister task queue and advancing rounds to trigger the interleaving at each `await` point.

### Citations

**File:** rs/sns/governance/src/governance.rs (L2132-2133)
```rust
        let governance: &'static mut Governance = unsafe { std::mem::transmute(self) };
        spawn_in_canister_env(governance.perform_action(proposal_id, action));
```

**File:** rs/sns/governance/src/governance.rs (L2828-2828)
```rust
        self.check_no_upgrades_in_progress(Some(proposal_id))?;
```

**File:** rs/sns/governance/src/governance.rs (L2830-2835)
```rust
        let current_version = self.get_or_reset_deployed_version().await.map_err(|err| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!("Could not execute proposal: {err}"),
            )
        })?;
```

**File:** rs/sns/governance/src/governance.rs (L2844-2851)
```rust
        } = get_upgrade_params(&*self.env, root_canister_id, &current_version)
            .await
            .map_err(|err| {
                GovernanceError::new_with_message(
                    ErrorType::InvalidProposal,
                    format!("Could not execute proposal: {err}"),
                )
            })?;
```

**File:** rs/sns/governance/src/governance.rs (L2859-2867)
```rust
        let target_wasm = get_wasm(&*self.env, new_wasm_hash.to_vec(), canister_type_to_upgrade)
            .await
            .map_err(|e| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!("Could not execute proposal: {e}"),
                )
            })?
            .wasm;
```

**File:** rs/sns/governance/src/governance.rs (L2880-2888)
```rust
            for target_canister_id in canister_ids_to_upgrade {
                self.upgrade_non_root_canister(
                    target_canister_id,
                    Wasm::Bytes(target_wasm.clone()),
                    Encode!().unwrap(),
                    CanisterInstallMode::Upgrade,
                )
                .await?;
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

**File:** rs/sns/governance/src/governance.rs (L5636-5646)
```rust
        self.proto.pending_version = Some(PendingVersion {
            target_version: Some(next_version.clone()),
            mark_failed_at_seconds: self.env.now() + 5 * 60,
            checking_upgrade_lock: 0,
            proposal_id: None,
        });

        println!("Initiating upgrade to version: {next_version:?}");
        let upgrade_attempt = self
            .upgrade_sns_framework_canister(wasm_hash, canister_type)
            .await;
```

**File:** rs/sns/governance/src/governance.rs (L5647-5652)
```rust
        if let Err(err) = upgrade_attempt {
            let message = format!("Upgrade attempt failed: {err}");
            log!(ERROR, "{}", message);
            self.proto.pending_version = None;
            self.invalidate_target_version(message);
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
