Audit Report

## Title
Cycles Minted Without ICP Burned on Transient Ledger Failure in CMC - (File: `rs/nns/cmc/src/main.rs`)

## Summary
`burn_and_log` is called after cycles have already been irreversibly deposited or minted, and it silently discards any error from the ICP ledger's `send_pb` call. If the ledger is transiently unavailable (e.g., during an NNS upgrade), the burn is skipped, the notification is permanently marked as successfully completed, and no retry mechanism exists. The result is cycles in existence without corresponding ICP destruction, breaking the protocol's conservation invariant.

## Finding Description
In `rs/nns/cmc/src/main.rs`, both `process_top_up` (L1985–2012) and `process_mint_cycles` (L1958–1983) follow the same pattern: first commit cycles delivery, then call `burn_and_log`. The function `burn_and_log` (L2014–2049) makes an inter-canister call to the ICP ledger via `call_protobuf(ledger_canister_id, "send_pb", send_args)` and on error only logs the failure — it returns `()` regardless. The design intent is explicit in the comment at L2014–2016:

> "Burning doesn't return errors - we don't want to reject the transaction notification because then it could be retried."

After `process_top_up` or `process_mint_cycles` returns `Ok(...)`, the caller (`notify_top_up` / `notify_mint_cycles`) permanently records the block as `NotifiedTopUp(Ok(cycles))` or `NotifiedMint(Ok(...))` in `blocks_notified` (L1214–1222, L1305–1313). The `is_transient_error` guard at L1219 and L1310 only removes the entry when the result is a retriable `Err` — an `Ok` result is never removed. Any subsequent call for the same block index returns the cached `Ok` result (L1187, L1276), so there is no path to retry the burn. The ICP remains in the CMC's subaccount with no automatic or manual recovery mechanism in the current code.

Exploit path:
1. Attacker sends ICP to the CMC subaccount derived from a target canister ID with `MEMO_TOP_UP_CANISTER`.
2. Attacker observes a public NNS proposal to upgrade the ICP ledger canister and its execution timing on-chain.
3. Attacker calls `notify_top_up` timed so that `deposit_cycles` (management canister call) completes just before the ledger stops, and `burn_and_log`'s `send_pb` call hits the stopped ledger.
4. `burn_and_log` receives a reject, logs it, and returns `()`.
5. `process_top_up` returns `Ok(cycles)`; `notify_top_up` stores `NotifiedTopUp(Ok(cycles))` permanently.
6. Cycles are in the target canister. The ICP is not burned. No retry is possible.

## Impact Explanation
Cycles are minted without the corresponding ICP being destroyed. The ICP-to-cycles conservation invariant — a core economic property of the Internet Computer protocol — is violated. The ICP is permanently locked in the CMC's subaccount (inaccessible to the user, but not burned), meaning the total ICP supply is higher than the protocol model requires. This constitutes illegal minting of cycles and a concrete, persistent accounting error in the NNS/CMC financial infrastructure. This matches the High impact class: "Significant NNS, SNS, or infrastructure security impact with concrete user or protocol harm."

## Likelihood Explanation
The ICP ledger is briefly stopped during NNS canister upgrades. NNS upgrade proposals are public and their execution timing is observable on-chain, making the upgrade window predictable. An unprivileged user can pre-position ICP in CMC subaccounts and call `notify_top_up` or `notify_mint_cycles` during the window. The attack requires no special privileges, no victim interaction, and no external dependencies beyond observing public NNS governance activity. The per-event window is narrow (seconds), but the attack is repeatable across multiple pre-staged transactions and across multiple upgrade events.

## Recommendation
The burn should not be fire-and-forget after cycles are committed. Two viable approaches:

1. **Retry via heartbeat**: Record failed burns in persistent CMC state (e.g., a `pending_burns: BTreeMap<Subaccount, Tokens>` field) and retry them in the CMC heartbeat until they succeed, similar to how `blocks_notified` tracks notification status.
2. **Burn-before-mint ordering**: Attempt the ICP burn first; only proceed with `deposit_cycles` / `do_mint_cycles` if the burn succeeds. This changes the atomicity model but eliminates the window entirely.

Either approach eliminates the silent-discard path. The current comment justifying the design ("we don't want to reject the transaction notification because then it could be retried") is addressed by approach 1 without requiring the burn to block the response.

## Proof of Concept
Minimal deterministic integration test plan using `StateMachine` / PocketIC:

1. Set up NNS canisters including CMC and ICP ledger.
2. Fund a test user with ICP; send ICP to the CMC subaccount for a target canister with `MEMO_TOP_UP_CANISTER`.
3. Pause/stop the ICP ledger canister (simulating an upgrade stop) using `state_machine.stop_canister(ledger_id)`.
4. Call `notify_top_up` for the block index from step 2.
5. Assert the call returns `Ok(cycles)` and the target canister's cycle balance increased.
6. Resume the ledger; query the CMC subaccount balance.
7. Assert the CMC subaccount still holds the original ICP (not burned), and `blocks_notified` contains `NotifiedTopUp(Ok(cycles))` for the block — confirming cycles were minted without ICP destruction.