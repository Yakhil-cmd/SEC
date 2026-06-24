Audit Report

## Title
SNS Governance Remains in `PreInitializationSwap` Mode After Swap Abort, Permanently Blocking Neuron Disburse/Dissolve Operations - (File: rs/sns/swap/src/swap.rs)

## Summary
When an SNS decentralization swap is aborted, the `finalize_inner` function in the swap canister returns early after restoring dapp controllers without ever calling `set_sns_governance_to_normal_mode`. This leaves the SNS governance canister permanently in `PreInitializationSwap` mode, which blocks all user-initiated neuron management commands including `Disburse`, `DisburseMaturity`, `Configure` (including `StartDissolving`/`StopDissolving`), `Split`, and `MergeMaturity`. Pre-existing neuron holders (developer/founding team neurons created at SNS initialization) cannot withdraw or manage their staked SNS tokens until the mode is manually reset via an NNS upgrade proposal.

## Finding Description

**Root cause — early return in `finalize_inner` on abort path:**

In `rs/sns/swap/src/swap.rs`, `finalize_inner` checks `should_restore_dapp_control()` which returns `true` when `lifecycle() == Lifecycle::Aborted`. When true, it restores dapp controllers and immediately returns at L1583, bypassing the `set_sns_governance_to_normal_mode` call at L1610–1612 that only executes on the committed path:

```
finalize_inner:
  sweep_icp → settle_neurons_fund → [if Aborted: restore_dapp_controllers → RETURN]
  → create_neuron_recipes → sweep_sns → claim_neurons → set_mode(Normal)  ← never reached on abort
```

**Blocked commands in `PreInitializationSwap` mode:**

`rs/sns/governance/src/types.rs` L182–211 implements `manage_neuron_command_is_allowed_in_pre_initialization_swap_or_err` with a `_ => false` catch-all. Only `Follow`, `MakeProposal`, `RegisterVote`, `AddNeuronPermissions`, `RemoveNeuronPermissions`, and `ClaimOrRefresh` (swap canister only) are permitted. All of `Disburse`, `DisburseMaturity`, `Configure`, `Split`, and `MergeMaturity` fall through to `_ => false`.

**Mode check applied before every `manage_neuron` command:**

`rs/sns/governance/src/governance.rs` L4781–4782 calls `allows_manage_neuron_command_or_err` before any neuron command is dispatched, so `disburse_neuron` (L1119+) is never reached.

**Confirmed by integration tests:**

`rs/nervous_system/integration_tests/tests/sns_lifecycle.rs` L1118–1127 explicitly asserts `Mode::PreInitializationSwap` after an aborted swap. `rs/tests/nns/sns/lib/src/swap_finalization.rs` L123–125 asserts `Mode::PreInitializationSwap` in `finalize_aborted_swap_and_check_success`. The test file comment at L128 documents this as the intended state machine: `{ governance::Mode::PreInitializationSwap } FinalizeUnSuccessfully { governance::Mode::PreInitializationSwap }`.

**Exploit path:**
1. An SNS is initialized with developer/founding team neurons (pre-existing neurons with SNS tokens staked).
2. NNS governance adopts a `CreateServiceNervousSystem` proposal, setting governance to `PreInitializationSwap` mode and opening the swap.
3. The swap fails (insufficient participation, timeout, or other abort condition — no privileged actor required).
4. `finalize_inner` runs the abort path, restores dapp controllers, and returns early without calling `set_mode(Normal)`.
5. SNS governance remains in `PreInitializationSwap` mode indefinitely.
6. Pre-existing neuron holders call `manage_neuron` with `Disburse`, `DisburseMaturity`, or `Configure::StartDissolving` — all return `PreconditionFailed` error.
7. Funds remain locked unless NNS governance passes an upgrade proposal to reset the mode.

## Impact Explanation

Pre-existing SNS neuron holders (developer/founding team) cannot disburse dissolved neurons, start dissolving neurons, claim maturity rewards, or split neurons. Their staked SNS tokens are held in neuron subaccounts on the SNS ledger and are inaccessible through the governance canister while it remains in `PreInitializationSwap` mode. If the SNS project is abandoned after a failed swap or NNS voters do not act, the funds are permanently inaccessible. This constitutes a significant SNS security impact with concrete user harm, matching the **High** bounty tier: "Significant SNS or infrastructure security impact with concrete user or protocol harm."

## Likelihood Explanation

A swap abort is a realistic, non-adversarial scenario: it occurs automatically when the minimum participation threshold is not met by the swap deadline. No privileged actor or malicious behavior is required — the condition arises from normal protocol flow. The only constraint is that the SNS must have pre-existing neurons (which is always true for developer/founding team neurons created at initialization). Recovery requires a subsequent NNS governance proposal to upgrade the SNS governance canister, which may not occur if the project is abandoned or NNS voters are unresponsive.

## Recommendation

1. In `finalize_inner` (`rs/sns/swap/src/swap.rs`), on the abort path (after `restore_dapp_controllers_for_finalize`), add a call to `set_sns_governance_to_normal_mode` before returning, analogous to the committed path.
2. Alternatively, modify `manage_neuron_command_is_allowed_in_pre_initialization_swap_or_err` in `rs/sns/governance/src/types.rs` to explicitly permit user exit/withdrawal commands (`Disburse`, `DisburseMaturity`, `Configure::StartDissolving`) in `PreInitializationSwap` mode, since these do not threaten swap integrity.
3. At minimum, add an explicit post-abort `set_mode(Normal)` call so the governance canister is always in a usable state after finalization regardless of outcome.

## Proof of Concept

**Step 1 — Confirm abort path skips `set_mode`:** Read `finalize_inner` in `rs/sns/swap/src/swap.rs` L1572–1583: `should_restore_dapp_control()` returns `true` for `Lifecycle::Aborted` (L1348–1350) and the function returns at L1583, before `set_sns_governance_to_normal_mode` at L1610.

**Step 2 — Confirm blocked commands:** Read `rs/sns/governance/src/types.rs` L182–211: `_ => false` blocks `Disburse`, `DisburseMaturity`, `Configure`, `Split`, `MergeMaturity`.

**Step 3 — Confirm mode check is enforced:** Read `rs/sns/governance/src/governance.rs` L4781–4782: mode check runs before any neuron command dispatch.

**Step 4 — Run existing integration test:** The test `rs/nervous_system/integration_tests/tests/sns_lifecycle.rs` with `SwapFinalizationStatus::Aborted` already asserts `Mode::PreInitializationSwap` at L1118–1127. Extend this test to additionally call `manage_neuron` with `Disburse` on a pre-existing developer neuron and assert it returns `PreconditionFailed` — this will pass, confirming the lock.

**Step 5 — Confirm aborted finalization response has `set_mode_call_result: None`:** `rs/nervous_system/integration_tests/src/pocket_ic_helpers.rs` L2936 shows `set_mode_call_result: None` in the expected aborted finalization response pattern, confirming `set_mode` is never called on abort.