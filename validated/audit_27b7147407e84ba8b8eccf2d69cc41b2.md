Audit Report

## Title
Unprivileged Caller Can Abort Any In-Progress SNS Upgrade via `fail_stuck_upgrade_in_progress` - (File: rs/sns/governance/canister/canister.rs)

## Summary
The `fail_stuck_upgrade_in_progress` update method on the SNS Governance canister performs no caller authorization check. Any principal — including the anonymous identity — can invoke it after the 5-minute deadline to clear `pending_version` and force the associated governance proposal into a `Failed` state. This can be repeated indefinitely to permanently block an SNS from upgrading its canisters.

## Finding Description
The canister endpoint at `rs/sns/governance/canister/canister.rs` L526–535 is a public `#[update]` method that passes the request directly to `governance_mut().fail_stuck_upgrade_in_progress(...)` with no `caller()` check. Contrast this with `set_mode` at L543–546, which passes `caller()` to the governance layer for authorization.

The implementation at `rs/sns/governance/src/governance.rs` L6328–6361 applies only a time-based guard: `if now > pending_version.mark_failed_at_seconds`. If the condition holds, it calls `complete_sns_upgrade_to_next_version` with `ExternalFailure` status. That function at L6280–6313 unconditionally sets `self.proto.pending_version = None` (L6309) and calls `self.set_proposal_execution_status(proposal_id, result)` (L6306), marking the proposal as failed.

The `mark_failed_at_seconds` deadline is set to `self.env.now() + 5 * 60` at L5638, meaning the attack window opens exactly 5 minutes after an upgrade starts. The developer comment at L6337–6338 explicitly acknowledges the missing guard: *"Maybe, we should look at the checking_upgrade_lock field and only proceed if it is false, or the request has force set to true."* No such check was added.

## Impact Explanation
This matches the allowed High impact: **"Significant SNS security impact with concrete user or protocol harm."** Any unprivileged attacker can permanently block an SNS from upgrading its canisters by repeatedly aborting each upgrade attempt after the 5-minute window. Each abort forces the SNS community to re-submit and re-vote on the upgrade proposal, while the attacker can nullify each attempt with a single zero-cost ingress call. This is a concrete, repeatable governance disruption attack against the SNS framework, which is explicitly in-scope.

## Likelihood Explanation
The attack requires no privileged access, no tokens, and no neuron. The only precondition is that an SNS upgrade proposal has been adopted and is executing, and that 5 minutes have elapsed. SNS upgrades involving inter-canister calls or transient failures can remain in `pending_version` state beyond the 5-minute window. The attack is trivially scriptable by polling `get_running_sns_version` for a non-null `pending_version` with an elapsed `mark_failed_at_seconds`, then calling `fail_stuck_upgrade_in_progress` from any identity.

## Recommendation
Add a caller authorization check to `fail_stuck_upgrade_in_progress` in `canister.rs` before delegating to the governance layer. The method should only be callable by a principal with governance authority (e.g., a neuron holder via `manage_neuron`, or restricted to the SNS governance canister itself via a `caller() == id()` check). At minimum, record the caller's principal ID in the upgrade journal entry so unauthorized invocations are auditable on-chain.

## Proof of Concept
1. Deploy an SNS locally (or use PocketIC) and submit an upgrade proposal that will remain in `pending_version` state for more than 5 minutes (e.g., by targeting a canister that does not respond promptly).
2. Advance the replica clock past `mark_failed_at_seconds` (5 minutes after upgrade start).
3. From the anonymous identity, call `fail_stuck_upgrade_in_progress` with an empty `FailStuckUpgradeInProgressRequest {}`.
4. Query `get_running_sns_version` and confirm `pending_version` is now `None`.
5. Query the governance proposal and confirm it is in `Failed` state.
6. Re-submit the upgrade proposal, wait for adoption, advance the clock past 5 minutes, and repeat step 3 to confirm the attack is indefinitely repeatable.