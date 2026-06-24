Audit Report

## Title
Unchecked ICP Burn Return Value in CMC Allows ICP Supply Inflation After Cycles Minting - (File: rs/nns/cmc/src/main.rs)

## Summary
The Cycles Minting Canister's `burn_and_log` function intentionally swallows all errors from the ICP ledger burn call and returns `()`. After cycles or a canister have been delivered, if the ICP ledger is temporarily unavailable (e.g., during an NNS-governed upgrade), the burn silently fails, the notification is permanently recorded as successful, and the corresponding ICP remains unburned in the CMC's subaccount — violating the protocol invariant that every minted cycle corresponds to destroyed ICP.

## Finding Description
In `rs/nns/cmc/src/main.rs`, `burn_and_log` (L2017–2049) issues a `send_pb` call to the ICP ledger but matches the `CallResult<BlockIndex>` only for logging, returning `()` in both the `Ok` and `Err` arms. The code comment at L2014–2016 explicitly acknowledges this:

> "Burning doesn't return errors - we don't want to reject the transaction notification because then it could be retried."

All three notification handlers call `burn_and_log` after the valuable operation succeeds and then unconditionally return `Ok(...)`:

- `process_create_canister` (L1943–1946): `burn_and_log(sub, amount).await; Ok(canister_id)`
- `process_mint_cycles` (L1967–1973): `burn_and_log(sub, amount).await; Ok(NotifyMintCyclesSuccess { ... })`
- `process_top_up` (L1999–2002): `burn_and_log(sub, amount).await; Ok(cycles)`

Because these functions return `Ok(...)`, the callers in `notify_top_up` (L1214–1222), `notify_mint_cycles` (L1305–1313), and `notify_create_canister` (L1411–1419) store the result as `NotificationStatus::NotifiedTopUp(Ok(...))` / `NotifiedMint(Ok(...))` / `NotifiedCreateCanister(Ok(...))`. The `is_transient_error` guard only removes the block from `blocks_notified` on transient errors; since the result is `Ok`, the block is permanently recorded as successfully processed. Any subsequent retry by the caller is rejected with `InvalidTransaction`.

The exploit path: the ICP ledger and cycles ledger are independent canisters. If the ICP ledger undergoes an NNS-governed upgrade between the `fetch_transaction` await point and the `burn_and_log` await point, `fetch_transaction` succeeds (ledger still live), `do_mint_cycles` / `deposit_cycles` succeeds (cycles ledger unaffected), and `burn_and_log`'s `send_pb` call is rejected (ICP ledger now upgrading). The CMC returns success; cycles are delivered; ICP remains in the CMC subaccount unburned.

## Impact Explanation
This is a concrete ICP supply invariant violation: cycles are minted without the corresponding ICP being destroyed. The ICP is not returned to the attacker — it is permanently stranded in the CMC's subaccount with no on-chain recovery path (the notification is finalized as `Ok`, so no retry is possible). The circulating ICP supply is permanently higher than the protocol requires. Repeated exploitation across multiple NNS ledger upgrades could accumulate a meaningful unburned ICP balance. This matches the allowed impact class: **illegal minting / protocol insolvency involving in-scope ledger assets** (CMC is a core NNS canister governing ICP↔cycles conversion). Severity: **High**, given the constraint of requiring a specific upgrade timing window.

## Likelihood Explanation
NNS upgrade proposals for the ICP ledger are submitted publicly and their execution timing is observable on-chain. An unprivileged caller can monitor the NNS governance canister for pending ICP ledger upgrade proposals, pre-fund the CMC subaccount, and submit `notify_mint_cycles` timed so that the ICP ledger upgrade fires between the two async await points. The window is narrow (one or two consensus rounds) but deterministic and predictable. No special privileges, leaked keys, or social engineering are required. The attacker does not receive "free" cycles — they pay ICP at the normal rate — but the ICP supply invariant is violated for the ecosystem. The attack is repeatable at every ICP ledger upgrade.

## Recommendation
1. **Return a `Result` from `burn_and_log`** and, on failure, clear the `blocks_notified` entry (allowing retry) only after reversing the cycles delivery via a compensating transaction to the cycles ledger or a two-phase commit pattern.
2. **Alternatively**, on burn failure, persist the unburned subaccount and amount in stable memory and retry the burn asynchronously on a heartbeat/timer, ensuring eventual consistency of the ICP supply invariant.
3. **At minimum**, emit a certified metric or stable-memory counter for unburned ICP amounts so that operators can detect and audit conservation violations.

## Proof of Concept
1. Caller sends ICP to `AccountIdentifier::new(CMC_ID, Subaccount::from(&caller()))` via `icrc1_transfer`. Block index `B` is recorded.
2. Monitor NNS governance for a pending ICP ledger upgrade proposal whose execution is imminent.
3. Submit `notify_mint_cycles({ block_index: B, to_subaccount: None, deposit_memo: None })` timed so that the ICP ledger upgrade fires between the `fetch_transaction` await (L1262) and the `burn_and_log` await (L1968).
4. `fetch_transaction` succeeds (ledger live); `do_mint_cycles` succeeds (cycles ledger unaffected); `burn_and_log`'s `send_pb` is rejected (ledger upgrading).
5. CMC stores `NotificationStatus::NotifiedMint(Ok(...))` (L1306–1308) and returns success.
6. Caller holds cycles; ICP remains in CMC subaccount; ICP supply is inflated by `amount`.

A deterministic integration test can reproduce this using PocketIC by: (a) setting up CMC + ICP ledger + cycles ledger, (b) stopping the ICP ledger canister between the two inter-canister calls in the CMC message execution, and (c) asserting that cycles were minted while the ICP ledger balance of the CMC subaccount remains non-zero and no burn block was recorded.