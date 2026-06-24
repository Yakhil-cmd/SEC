Audit Report

## Title
SNS Governance `pending_version` Lock Set After Async Upgrade Calls, Enabling Concurrent Re-execution - (File: rs/sns/governance/src/governance.rs)

## Summary
`perform_upgrade_to_next_sns_version_legacy` sets `self.proto.pending_version` only after all async cross-canister calls complete (line 2894), leaving the lock unset during the entire async execution window. Because the proposal remains in `Adopted` status throughout (the function returns `Ok(false)` and skips `set_proposal_execution_status`), and because `check_no_upgrades_in_progress` explicitly exempts the same proposal ID from its guard, a second timer-triggered invocation for the same proposal passes both guards and proceeds to upgrade SNS canisters a second time.

## Finding Description
`perform_upgrade_to_next_sns_version_legacy` begins with:

```rust
self.check_no_upgrades_in_progress(Some(proposal_id))?;  // line 2828
```

`check_no_upgrades_in_progress` (lines 2759–2786) has two sub-checks:
1. `upgrade_proposals_in_progress.is_subset(&{proposal_id})` — passes when the only in-progress proposal is the same one being re-entered, because `{X}.is_subset({X})` is always true.
2. `self.proto.pending_version.is_some()` — passes because `pending_version` is still `None`.

After the guard, the function makes four sequential async cross-canister calls (lines 2830, 2844, 2859, 2872–2888), each of which yields execution back to the IC scheduler. `pending_version` is only set at line 2894, after all calls succeed.

When `perform_action` receives `Ok(false)` from this function, it returns early (line 2168) without calling `set_proposal_execution_status`, so the proposal remains in `Adopted` status for the entire async window.

`run_periodic_tasks` is driven by a timer. If it fires while the first invocation is suspended at any `.await`, it calls `process_proposals` → `start_proposal_execution` for the same proposal (still `Adopted`). The second invocation passes both guards (same proposal ID exemption + `pending_version` still `None`) and proceeds through all async calls, upgrading the target SNS canister a second time.

By contrast, `initiate_upgrade_if_sns_behind_target_version` correctly sets `pending_version` at line 5636, before the async call at line 5644, and clears it on error at line 5650 — demonstrating the intended pattern.

## Impact Explanation
A concurrent second execution causes double WASM installation on an SNS canister (Ledger, Root, or Governance). If the canister's `post_upgrade` hook is not idempotent, this corrupts canister state. Both invocations eventually write to `self.proto.pending_version`, with the second overwriting the first, leaving SNS Governance with an inconsistent `deployed_version`/`pending_version` state that permanently blocks all future SNS upgrades. This matches the allowed High impact: **Significant SNS infrastructure security impact with concrete user or protocol harm**, including potential permanent DoS of the SNS upgrade mechanism.

## Likelihood Explanation
No external attacker is required. The IC's own timer scheduler triggers the condition whenever `run_periodic_tasks` fires during the async window of `perform_upgrade_to_next_sns_version_legacy`. The async window spans multiple cross-canister calls to SNS-W and Root, which are non-trivial in latency. On any normally operating subnet, the timer can fire during this window. The condition is repeatable for every `UpgradeSnsToNextVersion` proposal execution.

## Recommendation
Set `self.proto.pending_version` immediately after the initial guard check at line 2828, before the first `.await` at line 2830, mirroring the pattern in `initiate_upgrade_if_sns_behind_target_version` (line 5636). Clear it on any error path (as done at line 5650 in that function). This ensures the lock is held for the entire async execution window.

## Proof of Concept
1. An `UpgradeSnsToNextVersion` proposal (ID = X) is adopted; `start_proposal_execution` spawns `perform_upgrade_to_next_sns_version_legacy(X)` as a background future (F1).
2. F1 yields at `get_upgrade_params(...).await` (line 2844). At this point `pending_version` is `None` and proposal X is still `Adopted`.
3. The IC timer fires → `run_periodic_tasks` → `process_proposals` → `start_proposal_execution(X)` spawns F2.
4. F2 calls `check_no_upgrades_in_progress(Some(X))`: `{X}.is_subset({X})` → passes; `pending_version.is_some()` → `false` → passes.
5. F2 completes all async calls and installs the WASM onto the target SNS canister (second installation).
6. F1 resumes, completes its async calls, and installs the WASM again (first installation completes).
7. Both invocations write `pending_version`; the second overwrites the first, leaving governance in an inconsistent state.

A deterministic PocketIC integration test can reproduce this by: adopting an `UpgradeSnsToNextVersion` proposal, intercepting the response to `get_upgrade_params` to delay it, advancing the timer to trigger a second `run_periodic_tasks`, then releasing the delayed response and asserting that the target canister received two install calls and that `pending_version` is inconsistent.