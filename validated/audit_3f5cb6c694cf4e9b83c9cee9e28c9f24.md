Audit Report

## Title
SNS Swap Canister Permanently Locks Pre-Loaded SNS Tokens on Abort - (`rs/sns/swap/src/swap.rs`)

## Summary

The SNS Swap canister is pre-funded with SNS tokens before the swap opens. When a swap reaches `LIFECYCLE_ABORTED`, `finalize_inner` returns early after sweeping ICP back to buyers and restoring dapp controllers, but never transfers the pre-loaded SNS tokens back to the SNS governance canister. Those tokens remain permanently locked in the swap canister's SNS ledger account with no recovery path, constituting a ledger conservation violation for every failed SNS decentralization swap.

## Finding Description

**Pre-funding is confirmed by protocol documentation and code:**

The `LIFECYCLE_PENDING` enum comment states: *"Once SNS tokens have been transferred to the swap canister's account on the SNS ledger, a call to `open` with valid parameters will start the swap."* The proto documentation at `rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto` lines 115–117 confirms: *"Step 1 (State PENDING). The swap canister is loaded with the right amount of SNS tokens."*

**The abort path in `finalize_inner` never returns SNS tokens:**

In `rs/sns/swap/src/swap.rs` lines 1572–1584, when `should_restore_dapp_control()` returns `true` (i.e., lifecycle is `Aborted`), the function calls `restore_dapp_controllers_for_finalize` and then unconditionally returns:

```rust
if self.should_restore_dapp_control() {
    finalize_swap_response.set_set_dapp_controllers_result(
        self.restore_dapp_controllers_for_finalize(environment.sns_root_mut()).await,
    );
    return finalize_swap_response;  // Early return — sweep_sns never called
}
```

The `sweep_sns` call at lines 1593–1598 is only reached in the `COMMITTED` path. Furthermore, `sweep_sns` itself enforces this at lines 2170–2178 with an explicit lifecycle guard:

```rust
if self.lifecycle() != Lifecycle::Committed {
    log!(ERROR, "Halting sweep_sns(). SNS Tokens cannot be distributed if \
        Lifecycle is not COMMITTED. ...");
    return SweepResult::new_with_global_failures(1);
}
```

**No alternative recovery mechanism exists:**

- `error_refund_icp` only handles ICP refunds.
- ICRC-1 has no admin-transfer capability; SNS governance cannot forcibly pull tokens from the swap canister's ledger account.
- The swap canister exposes no API to transfer SNS tokens back to governance on abort.
- The lifecycle diagram (`ABORTED → <DELETED>`) confirms the canister is eventually deleted, making the tokens permanently inaccessible.

**Root cause:** The symmetric operation to `sweep_icp` (which returns ICP to buyers on abort) was never implemented for SNS tokens on the abort path.

## Impact Explanation

Every SNS decentralization swap that reaches `LIFECYCLE_ABORTED` results in the permanent destruction of the entire pre-loaded SNS token allocation. These tokens are minted/transferred into the swap canister's SNS ledger account and can never be recovered. This is a concrete ledger conservation violation: tokens enter the swap canister but have no exit path on abort. The financial impact scales with the token allocation and market value of the SNS token — for any non-trivial SNS project, this represents a significant loss of governance token supply. This matches the allowed impact: **"Significant SNS or infrastructure security impact with concrete user or protocol harm"** — High severity ($2,000–$10,000).

## Likelihood Explanation

Swap abortion is a normal, expected protocol outcome triggered whenever `min_participants` or `min_icp_e8s` is not reached before the swap deadline. No special privileges are required — any unprivileged user can cause a swap to abort simply by not participating (or by participating below the minimum threshold). This is passively triggerable, requires no exploit code, and is repeatable for every SNS project whose swap fails. Multiple real SNS swaps have historically aborted on mainnet, meaning this vulnerability has likely already caused token losses.

## Recommendation

In `finalize_inner`, before the early return on the `ABORTED` path, add a step to transfer the remaining SNS token balance from the swap canister's SNS ledger account back to the SNS governance canister's treasury account. This mirrors the existing `sweep_icp` pattern:

```rust
if self.should_restore_dapp_control() {
    // Return pre-loaded SNS tokens to SNS governance treasury
    self.sweep_sns_to_governance(now_fn, environment.sns_ledger()).await;

    finalize_swap_response.set_set_dapp_controllers_result(
        self.restore_dapp_controllers_for_finalize(environment.sns_root_mut()).await,
    );
    return finalize_swap_response;
}
```

The new `sweep_sns_to_governance` function should query the swap canister's SNS ledger balance and transfer the full amount (minus transaction fee) to `init.sns_governance` principal's default account.

## Proof of Concept

1. Deploy an SNS with a swap canister pre-loaded with `N` SNS tokens in `PENDING` state.
2. Open the swap (`OPEN` state) but ensure `min_icp_e8s` or `min_participants` is not reached before the deadline (e.g., zero or insufficient participation).
3. Call `try_abort` (or wait for the heartbeat) to transition the swap to `ABORTED`.
4. Call `finalize`. Observe in `finalize_inner`:
   - `sweep_icp` executes and returns all ICP to buyers.
   - `should_restore_dapp_control()` returns `true`.
   - `restore_dapp_controllers_for_finalize` executes.
   - Function returns early — `sweep_sns` is never invoked.
5. Query the swap canister's SNS ledger account balance: it still holds `N` SNS tokens.
6. Confirm no API exists to recover these tokens. Delete the swap canister and verify the tokens are permanently inaccessible.

A deterministic integration test using `PocketIC` or the existing `swap.rs` test harness can reproduce this by setting `lifecycle: Aborted as i32`, calling `finalize`, and asserting that the mock SNS ledger received zero transfer calls while the swap canister's SNS account balance remains non-zero.