Audit Report

## Title
Unchecked ICP Burn Result in CMC `burn_and_log` Allows Cycles Minting Without ICP Conservation - (File: rs/nns/cmc/src/main.rs)

## Summary
The Cycles Minting Canister mints cycles (or tops up a canister) before burning the corresponding ICP. The `burn_and_log` function explicitly discards all errors from the ICP ledger `send_pb` call, returning `()` unconditionally. If the burn fails, cycles are permanently issued without the backing ICP being destroyed, and the block index is marked as permanently processed with no retry path.

## Finding Description
In `rs/nns/cmc/src/main.rs`, `process_mint_cycles` (L1966–1973) calls `do_mint_cycles` first; on success it calls `burn_and_log` and immediately returns `Ok(NotifyMintCyclesSuccess{...})` regardless of whether the burn succeeded. The same pattern exists in `process_top_up` (L1999–2002). The function `burn_and_log` (L2017–2049) has return type `()`, and its error arm at L2044–2047 only calls `print(...)` — the error is never propagated. The design comment at L2015–2016 explicitly acknowledges this: *"Burning doesn't return errors - we don't want to reject the transaction notification because then it could be retried."* After `process_mint_cycles` returns `Ok(...)`, the caller at L1305–1312 inserts `NotificationStatus::NotifiedMint(Ok(...))` into `blocks_notified`, permanently sealing the block index as processed. The `is_transient_error` check at L1310 only removes the entry on transient errors in the `NotifyMintCyclesResult` itself — a burn failure is invisible to this check because `process_mint_cycles` already returned `Ok`. There is no persistent state for failed burns and no background retry timer.

## Impact Explanation
When `burn_and_log` fails: cycles are credited to the user in full; the ICP remains in CMC's subaccount permanently (the block index is irrevocably marked processed); and the ICP-to-cycles conservation invariant is broken — total ICP supply is higher than it should be relative to cycles issued. This constitutes illegal minting of cycles without corresponding ICP destruction, matching the High impact category: *"Significant ledger/NNS/SNS or infrastructure security impact with concrete user or protocol harm."* The per-event magnitude is bounded by the user's ICP amount, but the condition is permanent and unrecoverable per occurrence.

## Likelihood Explanation
The ICP ledger `send_pb` call can fail when: (1) the ICP ledger canister is mid-upgrade (stop/upgrade/start cycle), during which all incoming calls are rejected; (2) the ICP ledger's message queue is full under load; (3) the ledger traps on a bug. Scenario (1) is the most realistic: NNS ledger upgrades are publicly announced and occur on a regular cadence. The attacker must time `notify_mint_cycles` so that `do_mint_cycles` (cycles ledger) succeeds but the subsequent `burn_and_log` (ICP ledger) fails — i.e., the cycles ledger is available while the ICP ledger is not. This is a narrow but real window since the two ledgers are upgraded independently. The attacker cannot force this on demand but can observe upgrade proposals and attempt to race the window. Likelihood is low but non-zero, and the condition is not detectable or recoverable after the fact.

## Recommendation
1. **Retry queue**: On burn failure, persist `(subaccount, amount, block_index)` in CMC stable state and retry in a background timer (analogous to ckBTC/ckETH failed-mint queues).
2. **Reverse ordering**: Burn ICP first via `send_pb`; only mint cycles if the burn succeeds. If the burn fails, return an error — the block index remains unprocessed and the user can retry after the ledger recovers.
3. **At minimum, expose a metric**: Increment a stable counter on burn failure so operators can detect and manually remediate stuck ICP.

## Proof of Concept
1. Obtain ICP and transfer `X` ICP to CMC's subaccount for the caller principal with `MEMO_MINT_CYCLES`.
2. Monitor the NNS governance feed for a pending ICP ledger upgrade proposal that is about to execute.
3. In the upgrade window (ledger canister stopped, calls rejected), call `notify_mint_cycles` on CMC.
4. CMC executes `do_mint_cycles` → cycles ledger `deposit` succeeds → cycles credited to caller.
5. CMC calls `burn_and_log` → `call_protobuf(ledger_canister_id, "send_pb", ...)` returns `Err(...)` because the ledger is stopped.
6. `burn_and_log` logs the error and returns `()`. CMC records `NotificationStatus::NotifiedMint(Ok(...))` for the block index.
7. **Outcome**: Caller holds cycles worth `X` ICP. `X` ICP remains in CMC's subaccount. Block index is permanently processed. ICP supply is inflated by `X` tokens with no recovery path.

A deterministic integration test can reproduce this by: deploying CMC + ICP ledger + cycles ledger in a PocketIC environment; stopping the ICP ledger canister after `do_mint_cycles` returns (using PocketIC's canister-stop API mid-execution via a mock); calling `notify_mint_cycles`; asserting cycles were minted and ICP was not burned.