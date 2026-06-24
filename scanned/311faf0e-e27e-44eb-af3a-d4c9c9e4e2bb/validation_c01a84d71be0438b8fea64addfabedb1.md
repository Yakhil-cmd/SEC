### Title
Unchecked Ledger Burn Return Value in Cycles Minting Canister Silently Fails, Causing ICP Conservation Violation - (File: rs/nns/cmc/src/main.rs)

### Summary
The `burn_and_log` function in the Cycles Minting Canister (CMC) calls the ICP ledger's `send_pb` to burn ICP tokens after successfully minting cycles or creating a canister, but intentionally discards the error result. If the ledger call fails transiently, ICP that should be destroyed remains in the CMC's subaccount, violating ICP supply conservation. This is the direct IC analog of the Yearn Vaults unchecked ERC-20 transfer return value bug.

### Finding Description
In `rs/nns/cmc/src/main.rs`, the function `burn_and_log` is called after every successful cycles-minting operation to destroy the ICP that was exchanged for cycles. The function explicitly documents that it does not propagate errors:

```rust
/// Attempt to burn the funds.
/// Burning doesn't return errors - we don't want to reject the transaction
/// notification because then it could be retried.
async fn burn_and_log(from_subaccount: Subaccount, amount: Tokens) {
    ...
    let res: CallResult<BlockIndex> = call_protobuf(ledger_canister_id, "send_pb", send_args).await;
    match res {
        Ok(block) => print(format!("{msg} done in block {block}.")),
        Err((code, err)) => {
            let code = code as i32;
            print(format!("{msg} failed with code {code}: {err:?}"))
        }
    }
}
```

`burn_and_log` returns `()` regardless of whether the ledger burn succeeded or failed. It is called in three critical paths, all of which proceed as if the burn succeeded:

- `process_create_canister` (line 1945): after `do_create_canister` succeeds, calls `burn_and_log(sub, amount).await` — canister is created and cycles are deposited, but ICP may not be burned.
- `process_top_up` (line 2001): after `deposit_cycles` succeeds, calls `burn_and_log(sub, amount).await` — cycles are deposited to the target canister, but ICP may not be burned.
- `process_mint_cycles` (line 1968): after `do_mint_cycles` succeeds, calls `burn_and_log(sub, amount).await` — cycles are minted to the cycles ledger, but ICP may not be burned.

### Impact Explanation
**Vulnerability class: Ledger conservation bug.**

When `burn_and_log` fails (e.g., due to a transient ICP ledger rejection or temporary unavailability), the ICP tokens that should be destroyed remain in the CMC's operation-specific subaccount (e.g., `Subaccount::from(&canister_id)` for top-up, `Subaccount::from(&controller)` for canister creation). The cycles or canister have already been delivered to the user. The result is:

1. **ICP supply inflation**: ICP that should be burned persists, violating the conservation invariant that cycles minting is backed by ICP destruction.
2. **Stranded ICP in CMC subaccounts**: The unburned ICP accumulates in CMC-controlled subaccounts. While the notification deduplication (`blocks_notified`) prevents re-processing the same block, the ICP is not recoverable through normal protocol flows and is effectively locked.
3. **Cycles/ICP double-value**: A user receives full cycles value while the corresponding ICP is not removed from supply.

### Likelihood Explanation
The ICP ledger can transiently reject calls during canister upgrades, subnet stress, or message queue saturation. The CMC's `notify_top_up`, `notify_create_canister`, and `notify_mint_cycles` endpoints are callable by any unprivileged ingress sender. An attacker cannot directly force the ledger to fail, but the failure window is a natural operational condition. The design comment acknowledges the trade-off explicitly, confirming the silent-failure path is reachable in production.

### Recommendation
The burn failure should be recorded in persistent state (e.g., a `PendingBurn` queue) so that a background task can retry the burn. Alternatively, the CMC should expose a `retry_burn` endpoint callable by governance or operators to re-attempt failed burns. At minimum, a metric counter should be incremented on burn failure so the condition is observable and actionable. The current approach of only logging the failure means unburned ICP is silently lost from the conservation invariant with no recovery path.

### Proof of Concept
1. User sends ICP to CMC's top-up subaccount for canister `C` and calls `notify_top_up`.
2. CMC calls `process_top_up` → `deposit_cycles` succeeds → canister `C` receives cycles.
3. CMC calls `burn_and_log` → ICP ledger returns a transient error (e.g., `SysTransient` during upgrade).
4. `burn_and_log` logs the error and returns `()`. The notification is recorded as `NotifiedTopUp(Ok(cycles))`.
5. The ICP remains in `Subaccount::from(&canister_id)` of the CMC. The block is marked as processed, so it cannot be re-notified.
6. Canister `C` has received cycles; the corresponding ICP was never burned. ICP total supply is higher than it should be. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1943-1946)
```rust
    match do_create_canister(controller, cycles, subnet_selection, settings).await {
        Ok(canister_id) => {
            burn_and_log(sub, amount).await;
            Ok(canister_id)
```

**File:** rs/nns/cmc/src/main.rs (L1966-1969)
```rust
    match do_mint_cycles(to_account, cycles, deposit_memo).await {
        Ok(deposit_result) => {
            burn_and_log(sub, amount).await;
            Ok(NotifyMintCyclesSuccess {
```

**File:** rs/nns/cmc/src/main.rs (L1999-2002)
```rust
    match deposit_cycles(canister_id, cycles, true, limiter_to_use).await {
        Ok(()) => {
            burn_and_log(sub, amount).await;
            Ok(cycles)
```

**File:** rs/nns/cmc/src/main.rs (L2014-2049)
```rust
/// Attempt to burn the funds.
/// Burning doesn't return errors - we don't want to reject the transaction
/// notification because then it could be retried.
async fn burn_and_log(from_subaccount: Subaccount, amount: Tokens) {
    let msg = format!("Burning of {amount} ICPTs from subaccount {from_subaccount}");
    let minting_account_id = with_state(|state| state.minting_account_id);
    if minting_account_id.is_none() {
        print(format!("{msg} failed: minting_account_id not set"));
        return;
    }
    let minting_account_id = minting_account_id.unwrap();
    let ledger_canister_id = with_state(|state| state.ledger_canister_id);

    if amount < DEFAULT_TRANSFER_FEE {
        print(format!("{msg}: amount too small ({amount})"));
        return;
    }

    let send_args = SendArgs {
        memo: Memo::default(),
        amount,
        fee: Tokens::ZERO,
        from_subaccount: Some(from_subaccount),
        to: minting_account_id,
        created_at_time: None,
    };
    let res: CallResult<BlockIndex> = call_protobuf(ledger_canister_id, "send_pb", send_args).await;

    match res {
        Ok(block) => print(format!("{msg} done in block {block}.")),
        Err((code, err)) => {
            let code = code as i32;
            print(format!("{msg} failed with code {code}: {err:?}"))
        }
    }
}
```
