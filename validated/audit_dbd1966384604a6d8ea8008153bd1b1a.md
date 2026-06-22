### Title
Silent Burn Failure Allows ICP to Remain Unburned After Successful CMC Operations - (File: rs/nns/cmc/src/main.rs)

### Summary
The `burn_and_log` function in the Cycles Minting Canister (CMC) intentionally swallows ledger transfer errors after successful operations (`notify_top_up`, `notify_create_canister`, `notify_mint_cycles`). When the ICP burn transfer to the minting account fails, the error is only logged and execution continues. Because the notification is already marked as processed and cannot be retried, the ICP that should have been burned remains permanently in the CMC's subaccount, violating ledger conservation invariants.

### Finding Description

After a successful top-up, canister creation, or cycle mint, the CMC calls `burn_and_log` to burn the user's ICP by transferring it to the minting account. The function is explicitly designed to never propagate errors: [1](#0-0) 

The function calls `send_pb` on the ledger and, on failure, only prints a log message: [2](#0-1) 

This `burn_and_log` is called unconditionally after each successful operation path: [3](#0-2) [4](#0-3) [5](#0-4) 

By the time `burn_and_log` is called, the notification block index has already been recorded as `NotifiedTopUp`, `NotifiedCreateCanister`, or `NotifiedMint` in `blocks_notified`: [6](#0-5) 

This means the notification cannot be retried. If the burn fails, the ICP is permanently stranded in the CMC's per-user subaccount with no recovery path.

### Impact Explanation

**Ledger conservation bug.** ICP that should be burned (removed from total supply) remains in the CMC's subaccount indefinitely. Over time, repeated ledger transient failures during burn accumulate unburned ICP in CMC subaccounts. The total ICP supply is not reduced as the protocol intends, breaking the economic invariant that ICP is destroyed when converted to cycles. The stranded ICP cannot be recovered by the user (notification is consumed) and cannot be burned again (no retry path exists).

### Likelihood Explanation

The ICP ledger can return transient errors (`TemporarilyUnavailable`) or reject calls during high load or upgrades. Any such transient failure during the `burn_and_log` call — which is a cross-canister call to the ICP ledger — silently leaves ICP unburned. This is reachable by any unprivileged user who calls `notify_top_up`, `notify_create_canister`, or `notify_mint_cycles` during a period of ledger unavailability. The `refund_icp` path correctly propagates errors and allows retry via `is_transient_error`: [7](#0-6) 

But `burn_and_log` has no equivalent retry mechanism.

### Recommendation

Replace the fire-and-forget `burn_and_log` pattern with a recoverable approach. Options include:

1. **Retry on transient failure**: Store a pending burn in stable state and retry in a heartbeat, similar to how the CMC handles other deferred operations.
2. **Propagate the error**: Return an error from the notify functions when the burn fails, and remove the notification from `blocks_notified` so the user can retry (consistent with how `is_transient_error` already removes transient failures for the refund path).
3. **Use `created_at_time` deduplication**: Issue the burn with a deterministic `created_at_time` so that a retry of the same burn is deduplicated by the ledger.

### Proof of Concept

1. User sends ICP to CMC subaccount with `MEMO_TOP_UP_CANISTER`.
2. User calls `notify_top_up` — cycles are deposited to the target canister successfully.
3. CMC calls `burn_and_log`; the ICP ledger returns a transient error (e.g., during an upgrade window).
4. `burn_and_log` logs the error and returns `()`.
5. `notify_top_up` returns `Ok(cycles)` to the user.
6. The block index is stored as `NotifiedTopUp(Ok(cycles))` — permanently consumed.
7. The ICP remains in the CMC's subaccount for `canister_id` indefinitely. No mechanism exists to burn it retroactively. [8](#0-7)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1214-1221)
```rust
            with_state_mut(|state| {
                state.blocks_notified.insert(
                    block_index,
                    NotificationStatus::NotifiedTopUp(result.clone()),
                );
                if is_transient_error(&result) {
                    state.blocks_notified.remove(&block_index);
                }
```

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

**File:** rs/nns/cmc/src/main.rs (L1985-2012)
```rust
async fn process_top_up(
    canister_id: CanisterId,
    from: AccountIdentifier,
    amount: Tokens,
    limiter_to_use: CyclesMintingLimiterSelector,
) -> Result<Cycles, NotifyError> {
    let cycles = tokens_to_cycles(amount)?;

    let sub = Subaccount::from(&canister_id);

    print(format!(
        "Topping up canister {canister_id} by {cycles} cycles."
    ));

    match deposit_cycles(canister_id, cycles, true, limiter_to_use).await {
        Ok(()) => {
            burn_and_log(sub, amount).await;
            Ok(cycles)
        }
        Err(err) => {
            let refund_block = refund_icp(sub, from, amount, TOP_UP_CANISTER_REFUND_FEE).await?;
            Err(NotifyError::Refunded {
                reason: err.to_string(),
                block_index: refund_block,
            })
        }
    }
}
```

**File:** rs/nns/cmc/src/main.rs (L2014-2017)
```rust
/// Attempt to burn the funds.
/// Burning doesn't return errors - we don't want to reject the transaction
/// notification because then it could be retried.
async fn burn_and_log(from_subaccount: Subaccount, amount: Tokens) {
```

**File:** rs/nns/cmc/src/main.rs (L2040-2048)
```rust
    let res: CallResult<BlockIndex> = call_protobuf(ledger_canister_id, "send_pb", send_args).await;

    match res {
        Ok(block) => print(format!("{msg} done in block {block}.")),
        Err((code, err)) => {
            let code = code as i32;
            print(format!("{msg} failed with code {code}: {err:?}"))
        }
    }
```

**File:** rs/nns/cmc/src/main.rs (L2088-2096)
```rust
        let send_res: CallResult<BlockIndex> =
            call_protobuf(ledger_canister_id, "send_pb", send_args).await;
        let block = send_res.map_err(|(code, err)| {
            let code = code as i32;
            NotifyError::Other {
                error_code: NotifyErrorCode::RefundFailed as u64,
                error_message: format!("Refund to {to} failed with code {code}: {err}"),
            }
        })?;
```
