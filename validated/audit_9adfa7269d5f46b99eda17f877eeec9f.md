Audit Report

## Title
Silently Ignored Ledger Burn Failure in CMC Allows ICP Supply Inflation Without Burning - (File: rs/nns/cmc/src/main.rs)

## Summary
The `burn_and_log` function in the Cycles Minting Canister intentionally discards ledger burn errors, returning `()` regardless of outcome. After cycles are minted and deposited, if the ICP ledger burn fails, the notification block is permanently recorded as successfully processed (`NotifiedTopUp(Ok(...))` / `NotifiedCreateCanister(Ok(...))` / `NotifiedMint(Ok(...))`), the ICP remains unburned in the CMC's subaccount, and no retry path exists. This breaks the invariant that every minted cycle corresponds to destroyed ICP, inflating the ICP total supply.

## Finding Description
The root cause is in `burn_and_log` at lines 2014–2049 of `rs/nns/cmc/src/main.rs`. The function is explicitly documented to swallow errors:

```rust
/// Attempt to burn the funds.
/// Burning doesn't return errors - we don't want to reject the transaction
/// notification because then it could be retried.
async fn burn_and_log(from_subaccount: Subaccount, amount: Tokens) {
```

The `send_pb` call result is matched only for logging; both branches return `()`:

```rust
match res {
    Ok(block) => print(format!("{msg} done in block {block}.")),
    Err((code, err)) => {
        let code = code as i32;
        print(format!("{msg} failed with code {code}: {err:?}"))
    }
}
```

All three callers unconditionally return `Ok(...)` after `burn_and_log`:
- `process_top_up` (L2000–2002): `burn_and_log(sub, amount).await; Ok(cycles)`
- `process_create_canister` (L1944–1946): `burn_and_log(sub, amount).await; Ok(canister_id)`
- `process_mint_cycles` (L1967–1973): `burn_and_log(sub, amount).await; Ok(NotifyMintCyclesSuccess { ... })`

The `notify_top_up` handler at L1214–1222 then permanently stores `NotificationStatus::NotifiedTopUp(Ok(cycles))`. The `is_transient_error` guard at L1219–1221 only removes the block entry for transient errors — since the result is `Ok(...)`, the block is permanently finalized. No heartbeat or retry mechanism exists for pending burns.

The comment's rationale — "we don't want to reject the transaction notification because then it could be retried" — conflates two distinct operations: retrying cycle minting (dangerous, double-spend risk) and retrying the ICP burn (safe, necessary for supply conservation). These are not separated.

## Impact Explanation
This is a financial integrity / illegal minting impact. When `burn_and_log` fails:
1. Cycles are irreversibly minted and deposited to the target canister.
2. The corresponding ICP is not burned — the ICP total supply is not reduced.
3. The block index is permanently marked as processed — no user retry is possible.
4. The ICP is permanently stranded in the CMC's subaccount.

The ICP ledger's total supply diverges from the expected value. Cycles were effectively created without destroying ICP. This matches the allowed Critical impact: "Theft, permanent loss, illegal minting, or protocol insolvency involving exorbitant ICP/Cycles or in-scope chain-key/ledger assets." Repeated exploitation during predictable ledger upgrade windows could accumulate meaningful supply inflation.

## Likelihood Explanation
The trigger requires the ICP ledger to be temporarily unreachable between `deposit_cycles` succeeding and `burn_and_log` executing — a single async await point. This window opens during:
- **Routine NNS ledger canister upgrades** (occurring every few weeks, publicly observable on-chain).
- **Transient canister message queue overflow** under high ledger load — no privileged access required.
- **Inter-subnet messaging delays** on the NNS subnet.

An attacker observing an imminent ledger upgrade can time a `notify_top_up` call to land in this window. The attacker bears no financial loss (they receive their cycles regardless), so there is no disincentive to attempt this repeatedly across multiple upgrade events.

## Recommendation
`burn_and_log` should be changed to return `Result<BlockIndex, NotifyError>` and propagate failures to callers. If the burn fails, the notification block should not be finalized as `NotifiedTopUp(Ok(...))` — it should either remain as `Processing` (allowing retry) or be removed from `blocks_notified`. A `NotificationStatus::BurnPending` state with heartbeat-driven retry would cleanly separate the "cycles already minted, do not re-mint" invariant from the "burn must eventually succeed" requirement. The current comment's conflation of these two concerns should be resolved explicitly in code.

## Proof of Concept
1. User transfers ICP to the CMC's top-up subaccount for canister `C` with memo `MEMO_TOP_UP_CANISTER`.
2. User calls `notify_top_up { block_index, canister_id: C }` timed to coincide with a ledger upgrade window.
3. CMC executes `process_top_up` → `deposit_cycles` succeeds; cycles deposited to `C`.
4. CMC calls `burn_and_log` → `send_pb` call to ledger returns an error (ledger mid-upgrade).
5. `burn_and_log` logs the error, returns `()`.
6. `process_top_up` returns `Ok(cycles)`.
7. `notify_top_up` stores `NotifiedTopUp(Ok(cycles))` for `block_index`; `is_transient_error` returns false for `Ok`, so entry is permanent.
8. **Result**: User received cycles; ICP was not burned; block permanently processed; ICP supply inflated by `amount`.

A deterministic integration test can reproduce this by: deploying a mock ledger canister that returns a reject error on `send_pb`, calling `notify_top_up`, and asserting that (a) cycles were deposited, (b) the CMC subaccount balance is non-zero (ICP not burned), and (c) a second `notify_top_up` call for the same block returns `AlreadyProcessed`.