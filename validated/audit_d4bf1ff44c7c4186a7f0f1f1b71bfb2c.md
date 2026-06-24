Audit Report

## Title
Silent Burn Failure and Unconditional Early-Return in `burn_and_log` Allows Cycles Minting Without ICP Destruction - (File: rs/nns/cmc/src/main.rs)

## Summary
The `burn_and_log` function in the Cycles Minting Canister silently discards ledger burn errors and contains an unconditional early-return when `amount < DEFAULT_TRANSFER_FEE`, skipping the burn entirely. Because all three notification handlers (`process_create_canister`, `process_mint_cycles`, `process_top_up`) call `burn_and_log` as fire-and-forget after successfully dispensing cycles or creating a canister, cycles can be minted without the corresponding ICP ever being destroyed, violating the protocol invariant that `cycles_minted ∝ ICP_burned`.

## Finding Description
The `burn_and_log` function at lines 2017–2049 has two distinct failure modes:

**Path 1 — Deterministic early-return (lines 2027–2030):** If `amount < DEFAULT_TRANSFER_FEE`, the function prints a log message and returns `()` without making any ledger call. The ICP remains in the CMC's per-user subaccount unburned.

**Path 2 — Silent ledger error (lines 2042–2047):** If the `send_pb` call to the ICP ledger returns an error (e.g., `TemporarilyUnavailable` during an upgrade, or a reject), the error is only logged and the function returns `()`.

In both paths, the three callers at lines 1945, 1968, and 2001 have already successfully dispensed cycles or created a canister before `burn_and_log` is invoked. The return value is discarded and the notification is marked as successfully processed. The design comment at lines 2014–2016 confirms this is intentional: the CMC avoids propagating burn errors to prevent the notification from being retried (which would double-dispense cycles). However, this means the ICP conservation invariant is broken whenever the burn fails.

For Path 1, the trigger is deterministic: a user sends a Transfer of exactly `DEFAULT_TRANSFER_FEE - 1` e8s (9,999 e8s) to the CMC's top-up or create-canister subaccount. The CMC reads the `amount` from the ledger block (line 1606–1615), converts it to cycles via `tokens_to_cycles`, calls `deposit_cycles` or `do_create_canister` successfully, then calls `burn_and_log(sub, 9999_e8s)`. The guard at line 2027 fires, the burn is skipped, and the ICP sits permanently in the CMC subaccount.

## Impact Explanation
This is a protocol-level accounting violation: cycles are minted (dispensed) without the corresponding ICP being destroyed. The ICP is not returned to the user — it is permanently locked in the CMC's subaccount — but it is also not burned, so the total ICP supply is not reduced as the protocol requires. Repeated exploitation accumulates unburned ICP in CMC subaccounts, inflating the effective ICP supply relative to cycles minted. This constitutes illegal minting (cycles issued without ICP destruction) and a permanent loss of the burn invariant, matching the High impact class: significant NNS/ledger security impact with concrete protocol harm.

## Likelihood Explanation
Path 1 (amount < DEFAULT_TRANSFER_FEE) is trivially triggerable by any unprivileged user with no special conditions: send 9,999 e8s to a CMC subaccount and call `notify_top_up` or `notify_create_canister`. No timing, no race condition, no elevated privilege required. The per-transaction ICP amount is small (< 0.0001 ICP), but the attack is repeatable at negligible cost and the invariant violation accumulates. Path 2 (ledger unavailability) requires the ICP ledger to be temporarily unavailable (e.g., during NNS-governed upgrades), which occurs periodically and is observable on-chain.

## Recommendation
1. **Track unburned amounts in stable state:** Persist failed or skipped burn amounts (subaccount + amount) in a stable `BTreeMap`. A background timer task should retry these burns periodically, similar to how ckBTC handles `QuarantinedDeposit` entries.
2. **Remove the silent early-return for sub-fee amounts:** When `amount < DEFAULT_TRANSFER_FEE`, the amount should be recorded in stable state for later aggregation and burning rather than silently dropped.
3. **Emit a certified metric:** Expose the total unburned ICP balance as a certified canister metric so operators can detect and alert on accumulation.

## Proof of Concept
1. Obtain any canister ID `C` on mainnet or a local replica with the CMC deployed.
2. Send a Transfer of exactly 9,999 e8s from account `A` to `AccountIdentifier(CMC_ID, Subaccount::from(C))` with memo `MEMO_TOP_UP_CANISTER`. Record the block index `B`.
3. Call `notify_top_up({ block_index: B, canister_id: C })` on the CMC.
4. Observe: the call returns `Ok(cycles)` — cycles are deposited into `C`.
5. Query the ICP ledger for the balance of `AccountIdentifier(CMC_ID, Subaccount::from(C))`: it still holds 9,999 e8s (unburned).
6. Query the ICP ledger total supply: it has not decreased by 9,999 e8s, confirming the burn invariant is violated.
7. Repeat steps 2–6 with a fresh Transfer each time to accumulate unburned ICP indefinitely.