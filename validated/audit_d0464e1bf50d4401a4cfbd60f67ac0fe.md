Audit Report

## Title
Sequential Awaited Inter-Canister Calls Inside Sweep Loops Hold Finalization Lock for O(N) Rounds, Enabling Liveness DoS and Permanent Lock on Panic - (File: rs/sns/swap/src/swap.rs)

## Summary
The SNS Swap canister's `finalize` function acquires a boolean lock and delegates to `finalize_inner`, which calls `sweep_icp` and `sweep_sns`. Both sweep functions iterate over user-populated collections (`self.buyers`, `self.neuron_recipes`) and issue one sequentially-awaited inter-canister ledger call per entry. The lock is held across every `.await` boundary, meaning a swap with N participants holds `finalize_swap_in_progress = true` for O(N) consensus rounds. If the canister traps at any point inside `finalize_inner`, the lock is never released and all subsequent `finalize_swap` calls are permanently rejected until a canister upgrade.

## Finding Description

`finalize` acquires the lock at L1506, delegates to `finalize_inner` at L1512, and releases the lock at L1531. The code comment at L1528–1530 explicitly acknowledges: *"Note, if there is a panic, the lock will not be released. In that case, the Swap canister will need to be upgraded to release the lock."*

`finalize_inner` calls `sweep_icp` at L1558 and `sweep_sns` at L1596 sequentially.

`sweep_icp` (L2070–2151) iterates over `self.buyers` — a `BTreeMap` populated by any unprivileged caller via `refresh_buyer_tokens` during the OPEN phase — and for each entry calls `icp_transferable_amount.transfer_helper(...).await` at L2113–2121. `transfer_helper` (types.rs L625–633) calls `ledger.transfer_funds(...).await`, which is a real inter-canister call crossing a message boundary.

`sweep_sns` (L2199–2298) does the same over `self.neuron_recipes`, calling `sns_transferable_amount.transfer_helper(...).await` at L2266–2274.

On ledger failure, `transfer_helper` resets `transfer_start_timestamp_seconds = 0` at L655, so the same entry is retried on the next `finalize_swap` invocation, keeping the loop non-trivially long on retries.

The `AlreadyStarted` skip path (L617–619 in types.rs) only fires when `transfer_start_timestamp_seconds > 0`, i.e., when a prior call set the timestamp but did not yet complete. Successfully-transferred entries are not re-attempted. Failed entries are retried. This means the effective loop length on a retry pass equals the number of prior failures, not zero.

The number of entries in `self.buyers` is bounded by `max_icp_e8s / min_participant_icp_e8s`. If the SNS creator sets `min_participant_icp_e8s` to a small value, a coordinated group of principals can legitimately fill the map to a large size by committing the minimum ICP each.

## Impact Explanation

**Temporary liveness DoS (High):** With N buyers, a single `finalize_swap` invocation issues N sequential inter-canister calls, each requiring at least one consensus round (~1 s on the IC). A swap with 10,000 participants holds the lock for ≥ 10,000 rounds (~2.7 hours). During that window, every concurrent `finalize_swap` call returns immediately with "The Swap canister has finalize_swap call already in progress." Participants cannot trigger or observe finalization progress through the normal API path. This is a concrete, application-level DoS on an in-scope SNS governance component with direct user harm (delayed token/refund receipt).

**Permanent DoS on panic (High):** If any code path inside `finalize_inner` panics — due to an unexpected condition, a future regression, or an edge-case in a helper — `unlock_finalize_swap` is never called. `finalize_swap_in_progress` remains `Some(true)` in stable state. All subsequent `finalize_swap` calls are permanently rejected. Recovery requires a canister upgrade with a post-upgrade hook. This matches the "Application/platform-level DoS" and "Significant SNS security impact with concrete user or protocol harm" allowed impact classes.

## Likelihood Explanation

Any unprivileged principal can call `refresh_buyer_tokens` during the OPEN phase to register as a buyer. A coordinated group (or a single actor with many principals) can fill `self.buyers` to the maximum permitted by the swap parameters. The SNS creator controls `min_participant_icp_e8s`; if set to a small value, the cost per registered buyer is low. The temporary DoS is therefore straightforwardly triggerable by legitimate swap participants acting in concert. The permanent DoS requires a panic inside `finalize_inner`; while the developers have tried to prevent this (L1538–1543), it cannot be guaranteed against future regressions.

## Recommendation

1. **Batch and checkpoint sweeps:** Process buyers/recipes in bounded batches per `finalize_swap` invocation (analogous to the existing `CLAIM_SWAP_NEURONS_BATCH_SIZE` pattern). Persist a cursor into `self.buyers` / `self.neuron_recipes` so each call resumes where the previous one left off, releasing the lock between batches.
2. **Guaranteed lock release:** Wrap `finalize_inner` in a Rust `Drop`-based guard that calls `unlock_finalize_swap` unconditionally, even on panic, so a trap cannot permanently brick the canister.
3. **Instruction-aware early exit:** Check `ic_cdk::api::instruction_counter()` inside the sweep loops and break early when approaching the per-message limit, releasing the lock cleanly.
4. **Protocol-level participant cap:** Enforce a hard upper bound on the number of entries in `self.buyers` at the `refresh_buyer_tokens` call site, independent of the ICP-amount-based bound.

## Proof of Concept

1. Deploy an SNS swap with `min_participant_icp_e8s` set to a small value (e.g., 10,000,000 e8s = 0.1 ICP) and `max_icp_e8s` large enough to allow thousands of participants.
2. Have N distinct principals each call `refresh_buyer_tokens` during the OPEN phase, committing the minimum ICP, filling `self.buyers` with N entries.
3. Allow the swap to reach COMMITTED or ABORTED.
4. Call `finalize_swap` from principal A. Observe that the call begins issuing sequential ledger transfers.
5. Immediately call `finalize_swap` from principal B. Observe it returns immediately with "The Swap canister has finalize_swap call already in progress," confirming the lock-based liveness DoS.
6. For the permanent-lock path: write an integration test (e.g., using PocketIC) that injects a panic into a helper called from `finalize_inner` after the lock is acquired, then verify that all subsequent `finalize_swap` calls are permanently rejected without a canister upgrade.