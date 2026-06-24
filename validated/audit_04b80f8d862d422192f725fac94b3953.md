Audit Report

## Title
Silent Burn Failure Allows Cycles Minting Without ICP Destruction - (File: `rs/nns/cmc/src/main.rs`)

## Summary
The `burn_and_log` function in the Cycles Minting Canister unconditionally returns `()` regardless of whether the ICP ledger burn call succeeds or fails. Both `process_mint_cycles` and `process_top_up` call `burn_and_log` after successfully depositing cycles, then return `Ok(...)` and mark the notification as permanently processed. If the burn fails, cycles exist in the system without the corresponding ICP being destroyed, violating the ICP/cycles conservation invariant with no retry mechanism available.

## Finding Description
In `rs/nns/cmc/src/main.rs`, `burn_and_log` (L2017) is declared `async fn burn_and_log(...) -> ()` with an explicit design comment stating it never propagates errors. Inside, the `call_protobuf` result at L2040 is matched only for logging; errors are printed and discarded (L2044-2047).

`process_mint_cycles` (L1966-1974) calls `do_mint_cycles` first; on success it calls `burn_and_log(sub, amount).await` and immediately returns `Ok(NotifyMintCyclesSuccess { ... })`. `process_top_up` (L1999-2002) follows the identical pattern with `deposit_cycles` then `burn_and_log`. In both cases the `Ok` return is unconditional with respect to the burn outcome.

The caller (`notify_mint_cycles`, L1302-1313) stores `NotificationStatus::NotifiedMint(result.clone())` into `blocks_notified` after `process_mint_cycles` returns. The `is_transient_error` guard at L1310 only removes the entry if `result` itself is an `Err` variant — but since `process_mint_cycles` always returns `Ok` when cycles deposit succeeds, the entry is permanently committed. Any subsequent call with the same `block_index` hits the `Entry::Occupied` branch at L1274 and returns the cached success, making the notification non-retryable.

Exploit flow:
1. Attacker sends ICP to their CMC subaccount.
2. Attacker submits `notify_mint_cycles` (or `notify_top_up`).
3. `do_mint_cycles` / `deposit_cycles` completes — cycles are credited.
4. ICP ledger is stopped mid-upgrade; `burn_and_log`'s `send_pb` call returns a `CanisterStopped` reject.
5. `burn_and_log` logs the error and returns `()`.
6. `process_mint_cycles` returns `Ok(NotifyMintCyclesSuccess { ... })`.
7. `blocks_notified` is updated to `NotifiedMint(Ok(...))` — permanently processed.
8. Attacker holds newly minted cycles; ICP remains unburned in the CMC subaccount with no recovery path.

## Impact Explanation
This constitutes illegal minting of cycles: new cycles enter circulation without the corresponding ICP supply reduction that the CMC's economic model requires. The ICP/cycles exchange rate invariant enforced by the CMC is broken. Repeated exploitation across multiple upgrade windows (each NNS upgrade is publicly observable on-chain) can inflate the cycles supply by an amount proportional to the ICP the attacker is willing to commit per attempt. This matches the Critical/High allowed impact: "Theft, permanent loss, illegal minting, or protocol insolvency involving exorbitant ICP/Cycles or in-scope chain-key/ledger assets."

## Likelihood Explanation
The ICP ledger and CMC are both NNS canisters upgraded via publicly observable governance proposals. The upgrade window (canister stopping → upgrade → restart) is deterministic and visible on-chain. An unprivileged user with ICP can submit `notify_mint_cycles` timed so that the cycles-deposit step completes just before the ledger stops. The timing window is narrow (seconds) but predictable and repeatable across every ledger upgrade. No special privileges, social engineering, or external compromise is required.

## Recommendation
`burn_and_log` should be refactored to return `Result<BlockIndex, (RejectionCode, String)>` and propagate the error to its callers. If the burn fails, `process_mint_cycles` and `process_top_up` should either:
1. Retry the burn before returning (preferred — the cycles deposit is already committed and idempotent via cycles-ledger deduplication), or
2. Return a transient error so `is_transient_error` removes the entry from `blocks_notified`, allowing the user to resubmit (accepting that the cycles-deposit step is a no-op on retry due to deduplication).

The existing comment justification conflates two independent operations: the cycles deposit (idempotent) and the ICP burn (must succeed for conservation). They must be handled separately.

## Proof of Concept
1. Deploy a local replica with the NNS (CMC + ICP ledger).
2. Fund a test principal's CMC subaccount with ICP.
3. Intercept or mock the ICP ledger's `send_pb` endpoint to return a `CanisterStopped` reject (or stop the ledger canister before the burn step using PocketIC's `stop_canister`).
4. Call `notify_mint_cycles` with the corresponding `block_index`.
5. Assert: the call returns `Ok(NotifyMintCyclesSuccess { ... })`.
6. Assert: the cycles ledger balance of the test principal increased by the expected amount.
7. Assert: the ICP ledger balance of the CMC subaccount is unchanged (ICP not burned).
8. Assert: a second call with the same `block_index` returns the cached `Ok` (not retryable).

This can be implemented as a PocketIC integration test using `ic_cdk::api::call::reject` injection or by stopping the ledger canister between the `do_mint_cycles` await point and the `burn_and_log` await point via PocketIC's canister lifecycle controls.