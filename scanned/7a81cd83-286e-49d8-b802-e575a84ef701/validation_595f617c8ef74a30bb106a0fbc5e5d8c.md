### Title
SNS Governance `pending_version` Lock Set After Async Upgrade Calls, Enabling Concurrent Re-execution - (File: rs/sns/governance/src/governance.rs)

### Summary
In `perform_upgrade_to_next_sns_version_legacy`, the in-progress guard (`pending_version`) is set only **after** all async upgrade calls complete. During the async execution window, a concurrent invocation of the same function (for the same proposal) passes both guard checks and proceeds to upgrade SNS canisters a second time.

### Finding Description
`perform_upgrade_to_next_sns_version_legacy` in `rs/sns/governance/src/governance.rs` is the execution path for `UpgradeSnsToNextVersion` proposals. It begins with a guard check:

```rust
self.check_no_upgrades_in_progress(Some(proposal_id))?;
```

`check_no_upgrades_in_progress` has two sub-checks:

1. **`upgrade_proposals_in_progress` check** (line 2759–2772): returns an error if any upgrade proposal in `Adopted` status exists, **except** the one passed as `proposal_id`. Critically, `{X}.is_subset({X})` always passes, so the same proposal re-entering does not trigger this guard.

2. **`pending_version` check** (line 2775): returns an error if `self.proto.pending_version.is_some()`.

After the guard, the function makes multiple sequential async calls:
- `get_or_reset_deployed_version().await` (line 2830)
- `get_upgrade_params().await` (line 2844)
- `get_wasm().await` (line 2859)
- `upgrade_canister_directly().await` or `upgrade_non_root_canister().await` (lines 2872–2888)

Only **after all of these succeed** is the lock set:

```rust
self.proto.pending_version = Some(PendingVersion { ... }); // line 2894
```

Because IC canisters use cooperative multitasking, execution yields at every `.await`. During any of those yield points, `run_periodic_tasks` can be called again, which calls `process_proposals`, which can call `start_proposal_execution` for the same proposal (still in `Adopted` status). The second invocation of `perform_upgrade_to_next_sns_version_legacy` for the same `proposal_id`:

- Passes the `upgrade_proposals_in_progress` check (same proposal ID is the allowed exception)
- Passes the `pending_version` check (still `None` because the first invocation hasn't finished)
- Proceeds to fetch WASM and upgrade canisters again

By contrast, `initiate_upgrade_if_sns_behind_target_version` correctly sets `pending_version` **before** the async upgrade call (line 5636 precedes line 5644), demonstrating the intended pattern.

### Impact Explanation
A concurrent second execution of `perform_upgrade_to_next_sns_version_legacy` for the same proposal causes:
- Double installation of a WASM onto an SNS canister (e.g., Ledger, Root, Governance)
- Potential state corruption if the canister's `post_upgrade` hook is not idempotent
- Inconsistent `deployed_version` / `pending_version` state in SNS Governance, permanently blocking future upgrades
- Loss of upgrade journal integrity

### Likelihood Explanation
`run_periodic_tasks` is called on a timer. If the async calls in `perform_upgrade_to_next_sns_version_legacy` (cross-canister calls to SNS-W and Root) are slow or retried, the periodic task fires again during the window. This is a realistic scenario on a loaded subnet. No external attacker is required; the IC's own scheduler triggers the condition.

### Recommendation
Set `self.proto.pending_version` **before** the first async call in `perform_upgrade_to_next_sns_version_legacy`, mirroring the pattern already used in `initiate_upgrade_if_sns_behind_target_version` (line 5636). Clear it on error. This ensures the lock is held for the entire async execution window, not just after it completes.

### Proof of Concept

**Step 1**: An `UpgradeSnsToNextVersion` proposal (ID = X) is adopted and `start_proposal_execution` spawns `perform_upgrade_to_next_sns_version_legacy(X)` as a background future.

**Step 2**: The future yields at `get_upgrade_params(...).await` (line 2844). At this point `pending_version` is `None`.

**Step 3**: The IC scheduler runs `run_periodic_tasks` → `process_proposals` → `start_proposal_execution` for proposal X again (still `Adopted`).

**Step 4**: The second invocation calls `check_no_upgrades_in_progress(Some(X))`:
- `upgrade_proposals_in_progress()` returns `{X}`; `{X}.is_subset({X})` → passes
- `pending_version.is_some()` → `false` → passes

**Step 5**: The second invocation proceeds through all async calls and upgrades the target SNS canister a second time.

**Step 6**: Both invocations eventually set `pending_version`, with the second overwriting the first, leaving governance in an inconsistent state.

---

Relevant code locations: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/sns/governance/src/governance.rs (L2115-2134)
```rust
    ///
    /// The given proposal ID specifies the proposal and the `action` specifies
    /// what the proposal should do (basically, function and parameters to be applied).
    fn start_proposal_execution(&mut self, proposal_id: u64, action: Action) {
        // `perform_action` is an async method of &mut self.
        //
        // Starting it and letting it run in the background requires knowing that
        // the `self` reference will last until the future has completed.
        //
        // The compiler cannot know that, but this is actually true:
        //
        // - in unit tests, all futures are immediately ready, because no real async
        //   call is made. In this case, the transmutation to a static ref is abusive,
        //   but it's still ok since the future will immediately resolve.
        //
        // - in prod, "self" is a reference to the GOVERNANCE static variable, which is
        //   initialized only once (in canister_init or canister_post_upgrade)
        let governance: &'static mut Governance = unsafe { std::mem::transmute(self) };
        spawn_in_canister_env(governance.perform_action(proposal_id, action));
    }
```

**File:** rs/sns/governance/src/governance.rs (L2754-2789)
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
    }
```

**File:** rs/sns/governance/src/governance.rs (L2824-2828)
```rust
    async fn perform_upgrade_to_next_sns_version_legacy(
        &mut self,
        proposal_id: u64,
    ) -> Result<bool, GovernanceError> {
        self.check_no_upgrades_in_progress(Some(proposal_id))?;
```

**File:** rs/sns/governance/src/governance.rs (L2891-2899)
```rust
        // A canister upgrade has been successfully kicked-off. Set the pending upgrade-in-progress
        // field so that Governance's run_periodic_tasks logic can check on the status of
        // this upgrade.
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
