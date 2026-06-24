Audit Report

## Title
Stale `dapp_canister_ids` Entry After Canister Deletion Causes Unconditional Panic in `set_dapp_controllers`, Permanently Blocking SNS Swap Finalization - (File: `rs/sns/root/src/lib.rs`)

## Summary
`SnsRootCanister::set_dapp_controllers` performs a pre-flight `canister_status` check on every canister in the persisted `dapp_canister_ids` list. If any registered dapp canister has been deleted (e.g., exhausted its cycles), the management canister returns an error and the code unconditionally panics. Because the stale entry is only removed after a successful `update_settings` call — which is never reached — the panic fires on every subsequent invocation, permanently blocking SNS swap finalization and `DeregisterDappCanisters` governance proposals.

## Finding Description
`dapp_canister_ids` is a repeated field in the persisted `SnsRootCanister` proto state (`rs/sns/root/src/gen/ic_sns_root.pb.v1.rs`, line 29–30). Entries are added by `register_dapp_canister` (`rs/sns/root/src/lib.rs`, lines 740–744) and are only removed inside `set_dapp_controllers` at lines 872–879, after a successful `update_settings` call.

In the pre-flight loop (lines 796–826), for each `dapp_canister_id`, `canister_status` is called. The `Err(_)` arm at lines 807–812 unconditionally panics:

```rust
Err(_) => {
    // TODO(NNS1-1993): Remove this panic and return an error type instead.
    panic!(
        "Could not get the status of canister: {dapp_canister_id}.  Root may not be a controller."
    )
}
```

If a dapp canister has been deleted, `canister_status` returns `CanisterNotFound`. The panic fires before any removal logic is reached (lines 872–879), so the stale `PrincipalId` remains in `dapp_canister_ids` across upgrades. Every future call to `set_dapp_controllers` — including the swap canister's finalization path and governance's `DeregisterDappCanisters` path — hits the same panic. There is no recovery path short of a manual state migration via canister upgrade. The existing `TODO(NNS1-1993)` comment in the code itself acknowledges this is a known deficiency.

## Impact Explanation
This matches the allowed High impact: **"Significant SNS security impact with concrete user or protocol harm."** Specifically: the SNS swap cannot finalize (dapp canisters remain permanently locked under SNS root control with no transfer path), and the `DeregisterDappCanisters` governance proposal path is also blocked for the entire SNS instance. All surviving dapp canisters are effectively frozen under SNS root with no recovery mechanism available to token holders or the SNS governance system.

## Likelihood Explanation
Dapp canisters registered with SNS root can run out of cycles through normal operation — insufficient top-up, high traffic, or deliberate neglect. The IC automatically deletes canisters that remain below the freezing threshold for a grace period. This is a realistic, non-adversarial scenario requiring no privileged access. An adversary who can call any cycles-consuming public method on a dapp canister can also accelerate depletion. Only one dapp canister in the registered list needs to be deleted to trigger the permanent block.

## Recommendation
In the pre-flight loop of `set_dapp_controllers` (lines 807–812), handle `canister_status` errors gracefully instead of panicking. The preferred fix (as the `TODO(NNS1-1993)` comment already acknowledges) is to return a structured error. Additionally, a `CanisterNotFound` error should prune the stale entry from `dapp_canister_ids` and continue processing remaining canisters, rather than aborting the entire operation. The removal logic already present at lines 872–879 (`swap_remove_if`) can be reused for this purpose.

## Proof of Concept
1. Deploy an SNS. Register dapp canister `D` via `register_dapp_canisters`. `D` is added to `dapp_canister_ids` (line 743).
2. Allow `D` to exhaust its cycles; the IC deletes it. `dapp_canister_ids` still contains `D`'s `PrincipalId` (persisted proto state, line 30 of `pb.v1.rs`).
3. The swap canister calls `set_dapp_controllers` with `canister_ids: None` (swap finalization path, line 784).
4. The pre-flight loop (line 796) reaches `D` and calls `management_canister_client.canister_status(D)` (line 804).
5. The management canister returns `CanisterNotFound`; the `Err(_)` arm fires (line 807) and panics (line 809–811).
6. The SNS root canister traps. `dapp_canister_ids` is unchanged. Every retry hits the same panic.

A deterministic integration test using PocketIC can reproduce this by: registering a dapp canister, stopping and deleting it via the management canister, then calling `set_dapp_controllers` and asserting the canister traps.