### Title
Silent ICP Burn Failure in `burn_and_log` Allows Cycles Minting Without ICP Destruction - (File: `rs/nns/cmc/src/main.rs`)

---

### Summary

The Cycles Minting Canister (CMC) intentionally swallows errors from the ICP burn operation in `burn_and_log`. If the burn call to the ICP ledger fails after cycles have already been minted or deposited, the ICP is not destroyed but the notification is permanently cached as successful. This creates a ledger conservation violation: cycles exist without the corresponding ICP being burned.

---

### Finding Description

In `rs/nns/cmc/src/main.rs`, the function `burn_and_log` is called after every successful cycle-minting operation (`process_top_up`, `process_create_canister`, `process_mint_cycles`). Its purpose is to burn the ICP from CMC's subaccount by sending it to the minting account via the ICP ledger's `send_pb` endpoint.

The function is explicitly designed to swallow errors: [1](#0-0) 

The comment at line 2014–2016 reads: *"Burning doesn't return errors - we don't want to reject the transaction notification because then it could be retried."*

The call to `call_protobuf(ledger_canister_id, "send_pb", send_args)` can fail (e.g., ledger queue full, ledger temporarily unavailable, canister upgrade in progress). When it does, the error is only printed — the function returns `()` regardless. [2](#0-1) 

Because `burn_and_log` always returns `()`, the callers (`process_top_up`, `process_create_canister`, `process_mint_cycles`) return `Ok(...)` regardless of whether the burn succeeded: [3](#0-2) [4](#0-3) [5](#0-4) 

The `notify_top_up` handler then caches the result permanently as `NotifiedTopUp(Ok(cycles))`: [6](#0-5) 

The `is_transient_error` function only removes the cache entry for `Err` results that are retriable: [7](#0-6) 

Since the result is `Ok(...)`, the notification is permanently cached as successful. The ICP remains in CMC's subaccount indefinitely — it cannot be burned again (the notification is consumed), and it cannot be refunded to the user (the operation succeeded from CMC's perspective).

This is the direct IC analog of the ERC20 unsafe transfer pattern: just as `sendAllFundsToLP()` used a transfer call whose failure could be silently ignored for certain token types, `burn_and_log` uses a ledger transfer call whose failure is intentionally swallowed, leaving funds in an intermediate account without the corresponding destruction.

---

### Impact Explanation

**Vulnerability class: Ledger conservation bug.**

When `burn_and_log` fails:
1. Cycles have already been minted and deposited to the target canister or cycles ledger account.
2. ICP is **not** burned — it remains in CMC's subaccount (e.g., `Subaccount::from(&canister_id)` for top-ups).
3. The notification block index is permanently cached as `NotifiedTopUp(Ok(...))` / `NotifiedCreateCanister(Ok(...))` / `NotifiedMint(Ok(...))`.
4. The ICP in CMC's subaccount is permanently inaccessible: the user cannot re-notify (the block is consumed), and CMC has no mechanism to retry failed burns.

The result is that the total ICP supply is not reduced by the amount that should have been burned, while the corresponding cycles exist in the system. This violates the ICP↔cycles conservation invariant that underpins the economic model of the Internet Computer.

---

### Likelihood Explanation

The ICP ledger is a high-traffic canister. Transient failures of `send_pb` can occur due to:
- `CanisterQueueFull` (the ledger's input queue is saturated during high load)
- Ledger canister upgrade windows
- Subnet-level transient errors

Any unprivileged user who calls `notify_top_up`, `notify_create_canister`, or `notify_mint_cycles` during a window where the ledger is transiently unavailable will trigger this condition. The user does not need to cause the ledger failure — they only need to time their notification during a naturally occurring transient failure. Likelihood is **low** per individual call but non-zero at scale.

---

### Recommendation

1. **Track failed burns in CMC state.** Add a `pending_burns: BTreeMap<Subaccount, Tokens>` field to CMC state. If `burn_and_log` fails, record the pending burn. A background timer task (`canister_heartbeat` or `ic_cdk_timers`) should retry pending burns.

2. **Alternatively**, use a two-phase commit: do not cache the notification as fully successful until the burn is confirmed. Mark it as `Processing` until the burn ledger call returns `Ok`, then transition to `NotifiedTopUp(Ok(...))`.

3. At minimum, expose a privileged endpoint that allows CMC operators to manually trigger a retry of a failed burn for a given subaccount.

---

### Proof of Concept

**Entry path** (unprivileged ingress sender):

1. User sends ICP to CMC's subaccount for canister `C` with `MEMO_TOP_UP_CANISTER`.
2. User calls `notify_top_up { block_index: B, canister_id: C }` on the CMC canister.
3. CMC calls `process_top_up` → `deposit_cycles(C, cycles, ...)` → **succeeds** (cycles deposited to `C`).
4. CMC calls `burn_and_log(sub, amount)` → `call_protobuf(ledger_id, "send_pb", ...)` → **fails** with `(SysTransient, "CanisterQueueFull")` due to transient ledger unavailability.
5. `burn_and_log` logs the error and returns `()`.
6. `process_top_up` returns `Ok(cycles)`.
7. `notify_top_up` inserts `NotifiedTopUp(Ok(cycles))` into `blocks_notified`. `is_transient_error` returns `false` (result is `Ok`), so the entry is **not** removed.
8. **Result**: Canister `C` has received cycles. ICP remains in CMC's subaccount for `C`. The notification is permanently consumed. The ICP is neither burned nor refundable. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1125-1130)
```rust
fn is_transient_error<T>(result: &Result<T, NotifyError>) -> bool {
    if let Err(e) = result {
        return e.is_retriable();
    }
    false
}
```

**File:** rs/nns/cmc/src/main.rs (L1214-1222)
```rust
            with_state_mut(|state| {
                state.blocks_notified.insert(
                    block_index,
                    NotificationStatus::NotifiedTopUp(result.clone()),
                );
                if is_transient_error(&result) {
                    state.blocks_notified.remove(&block_index);
                }
            });
```

**File:** rs/nns/cmc/src/main.rs (L1943-1946)
```rust
    match do_create_canister(controller, cycles, subnet_selection, settings).await {
        Ok(canister_id) => {
            burn_and_log(sub, amount).await;
            Ok(canister_id)
```

**File:** rs/nns/cmc/src/main.rs (L1966-1973)
```rust
    match do_mint_cycles(to_account, cycles, deposit_memo).await {
        Ok(deposit_result) => {
            burn_and_log(sub, amount).await;
            Ok(NotifyMintCyclesSuccess {
                block_index: deposit_result.block_index,
                minted: cycles.into(),
                balance: deposit_result.balance,
            })
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
