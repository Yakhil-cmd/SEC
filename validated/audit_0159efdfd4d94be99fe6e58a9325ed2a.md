Audit Report

## Title
Silent ICP Burn Failure in CMC Allows Cycles Minting Without Ledger Conservation - (File: rs/nns/cmc/src/main.rs)

## Summary

The `burn_and_log` function in the Cycles Minting Canister is explicitly designed to swallow all errors from the ICP ledger `send_pb` call. After cycles are irreversibly minted and delivered to the target canister or cycles ledger, if the corresponding ICP burn fails, the error is only printed and the function returns normally. The notification is then permanently finalized as a success, preventing any retry or recovery. This breaks the ICP/cycles conservation invariant: cycles are created without the corresponding ICP being destroyed.

## Finding Description

The execution flow is as follows:

1. In `process_top_up` (L1999–2002), `deposit_cycles` is called with `mint_cycles: true`, which internally calls `ensure_balance` (L2116–2117). `ensure_balance` calls `ic0_mint_cycles128` (L2322) to mint new cycles into the CMC's own balance, then the cycles are transferred to the target canister via `call_with_payment128`. This is irreversible.

2. Only after successful cycle delivery does `burn_and_log(sub, amount).await` execute (L2001). The function signature is `async fn burn_and_log(from_subaccount: Subaccount, amount: Tokens)` — it returns `()` unconditionally (L2017). The error branch at L2044–2047 only calls `print(...)` and falls through, returning `()` as if the burn succeeded.

3. Back in `notify_top_up`, `process_top_up` returns `Ok(cycles)`. The `with_state_mut` block at L1214–1222 inserts `NotificationStatus::NotifiedTopUp(Ok(cycles))` into `blocks_notified`. The `is_transient_error` check at L1219 only removes the entry for transient errors — `Ok(cycles)` is not a transient error, so the block index is permanently finalized.

4. The same pattern applies identically in `process_create_canister` (L1944–1946) and `process_mint_cycles` (L1967–1973), with their respective notification finalization paths.

If the ledger `send_pb` call at L2040 fails for any reason — transient `SysTransient` reject, ledger queue overflow, or insufficient subaccount balance — the ICP remains in the CMC's subaccount, the cycles have already been delivered, and the block index is permanently marked as processed. There is no on-chain mechanism to recover or retry the burn.

## Impact Explanation

This is a concrete conservation violation: cycles are minted (ICP supply should decrease by the equivalent amount) but the ICP is not burned. The ICP total supply is permanently higher than it should be per failed burn. The stranded ICP in the CMC's subaccount cannot be reclaimed by the original sender (notification is finalized) and is not redistributed. This constitutes illegal minting of cycles relative to the ICP burned, directly harming the economic invariant underpinning the ICP/cycles exchange. This matches the High impact class: "Significant NNS, SNS, or infrastructure security impact with concrete user or protocol harm."

## Likelihood Explanation

Any unprivileged user calling `notify_top_up`, `notify_create_canister`, or `notify_mint_cycles` can trigger this path. No special privilege is required. The ICP ledger is a trusted NNS canister co-located on the NNS subnet with the CMC, making outright failures uncommon but not impossible. Realistic triggers include: (1) `SysTransient` reject codes from the IC runtime under temporary resource exhaustion or message queue overflow; (2) ledger-side `InsufficientFunds` rejection if the CMC's subaccount balance is lower than expected due to a prior failed burn accumulating in the same subaccount; (3) CMC upgrade between `deposit_cycles` and `burn_and_log` causing the in-flight state to be inconsistent. The vulnerability is repeatable across any number of transactions and accumulates over time.

## Recommendation

1. **Retry failed burns persistently**: Store failed burn attempts (subaccount, amount) in durable CMC state. A background timer task should retry them independently of the notification idempotency mechanism. The notification remains finalized (preventing double-minting of cycles) while the burn is retried until it succeeds.
2. **Emit a certified metric on burn failure**: Replace the `print` statement with a certified counter or structured metric observable on-chain, enabling operators to detect and respond to conservation violations.
3. **Pre-verify subaccount balance**: Before calling `ensure_balance`/`deposit_cycles`, verify that the CMC's subaccount holds at least `amount` ICP, reducing the chance of a burn failure due to balance mismatch.

## Proof of Concept

1. User sends N ICP to the CMC's subaccount for Canister X (with `MEMO_TOP_UP_CANISTER`), recorded at block index B.
2. User calls `notify_top_up { block_index: B, canister_id: X }`.
3. CMC executes `process_top_up` → `deposit_cycles` → `ensure_balance` mints cycles → cycles transferred to Canister X. Irreversible.
4. CMC calls `burn_and_log(sub_X, N_ICP)`.
5. Inject a transient failure: the ICP ledger's `send_pb` returns `Err((SysTransient, ...))`. This can be simulated in a PocketIC or local replica test by intercepting the outgoing call to the ledger canister and returning a reject.
6. `burn_and_log` prints the error and returns `()`.
7. `process_top_up` returns `Ok(cycles)`.
8. `notify_top_up` stores `NotifiedTopUp(Ok(cycles))` at block index B.
9. **Observable invariant violation**: Canister X has received cycles equivalent to N ICP. N ICP remains in the CMC's subaccount for Canister X, unburned. `total_cycles_minted` has been incremented but no corresponding ICP was destroyed. Calling `notify_top_up` again for block B returns the cached `Ok(cycles)` — no retry of the burn is possible.
10. A deterministic integration test using PocketIC can verify this by: (a) mocking the ledger to return a transient error on `send_pb`, (b) calling `notify_top_up`, (c) asserting that `blocks_notified[B]` is `NotifiedTopUp(Ok(...))` and that the CMC subaccount still holds the original ICP balance.