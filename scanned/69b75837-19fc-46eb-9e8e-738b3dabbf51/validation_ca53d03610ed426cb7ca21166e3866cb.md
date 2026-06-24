### Title
Unchecked Return Value of ICP Ledger Burn Call in CMC Silently Locks ICP and Violates Conservation - (File: rs/nns/cmc/src/main.rs)

---

### Summary

The `burn_and_log` function in the Cycles Minting Canister (CMC) intentionally swallows errors from the ICP ledger burn call. When the burn fails, the CMC still marks the notification as successfully processed, permanently locking ICP in the CMC's subaccount with no recovery path and violating ICP supply conservation.

---

### Finding Description

`burn_and_log` is called after a successful cycles deposit or canister creation to burn the corresponding ICP from the CMC's subaccount:

```rust
// rs/nns/cmc/src/main.rs:2017-2048
async fn burn_and_log(from_subaccount: Subaccount, amount: Tokens) {
    ...
    let res: CallResult<BlockIndex> = call_protobuf(ledger_canister_id, "send_pb", send_args).await;
    match res {
        Ok(block) => print(format!("{msg} done in block {block}.")),
        Err((code, err)) => {
            let code = code as i32;
            print(format!("{msg} failed with code {code}: {err:?}"))  // error silently dropped
        }
    }
}
```

The function returns `()` regardless of success or failure. Its three callers — `process_top_up`, `process_create_canister`, and `process_mint_cycles` — all proceed to return `Ok(...)` after `burn_and_log` returns, regardless of whether the burn succeeded:

```rust
// rs/nns/cmc/src/main.rs:1999-2002
match deposit_cycles(canister_id, cycles, true, limiter_to_use).await {
    Ok(()) => {
        burn_and_log(sub, amount).await;   // burn failure silently ignored
        Ok(cycles)                          // always returns Ok
    }
```

After `process_top_up` returns `Ok(cycles)`, the notification is permanently recorded as `NotifiedTopUp(Ok(cycles))`:

```rust
// rs/nns/cmc/src/main.rs:1214-1222
with_state_mut(|state| {
    state.blocks_notified.insert(
        block_index,
        NotificationStatus::NotifiedTopUp(result.clone()),
    );
    ...
});
```

This means the block index is consumed and cannot be re-notified. The ICP remains in the CMC's subaccount (keyed by `Subaccount::from(&canister_id)`) with no mechanism to retry or recover it.

The design comment acknowledges this trade-off: *"Burning doesn't return errors - we don't want to reject the transaction notification because then it could be retried."* However, the consequence is that a transient ledger failure permanently locks ICP.

---

### Impact Explanation

**Ledger conservation bug.** ICP that should be burned (destroyed) to balance the minted cycles is not burned. The total ICP supply is higher than the protocol intends. The locked ICP in the CMC's subaccount is inaccessible: only the CMC can spend from its own subaccounts, and no sweep or retry function exists. This affects all three notify paths: `notify_top_up`, `notify_create_canister`, and `notify_mint_cycles`.

---

### Likelihood Explanation

The ICP ledger can transiently reject calls during:
- Ledger canister upgrades (the canister is stopped)
- NNS subnet congestion or message queue saturation
- Any transient `CanisterError` or `SysTransient` reject

These are realistic operational events. Any user who calls `notify_top_up` (or the other notify endpoints) during such a window will trigger the silent failure. The attacker does not need to control the ledger failure — they only need to submit a valid notification during a window when the ledger is transiently unavailable.

---

### Recommendation

Track failed burns in persistent state and retry them in a background task (e.g., the existing `ProcessLogic` heartbeat pattern used by ckBTC). Alternatively, record the unburned subaccount balance and expose a permissionless `retry_burn` endpoint that can be called after the ledger recovers. The current design correctly avoids double-minting by not propagating errors to the caller, but it should separately ensure eventual burn completion.

---

### Proof of Concept

1. User sends 10 ICP to `AccountIdentifier::new(CMC_ID, Some(Subaccount::from(&canister_id)))`.
2. User calls `notify_top_up { block_index, canister_id }`.
3. CMC calls `deposit_cycles` → management canister deposits cycles to `canister_id` successfully.
4. CMC calls `burn_and_log(Subaccount::from(&canister_id), amount)`.
5. The ICP ledger is transiently unavailable (e.g., mid-upgrade); `call_protobuf` returns `Err(...)`.
6. `burn_and_log` logs the error and returns `()`.
7. `process_top_up` returns `Ok(cycles)`.
8. `notify_top_up` records `NotificationStatus::NotifiedTopUp(Ok(cycles))` for `block_index`.
9. The user received their cycles. The 10 ICP remains in the CMC's subaccount, unburned, permanently locked, and the ICP supply is inflated by 10 ICP.
10. Any subsequent call to `notify_top_up` with the same `block_index` returns the cached `Ok(cycles)` without attempting another burn. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1209-1226)
```rust
    match maybe_early_result {
        Some(result) => result,
        None => {
            let result = process_top_up(canister_id, from, amount, limiter_to_use).await;

            with_state_mut(|state| {
                state.blocks_notified.insert(
                    block_index,
                    NotificationStatus::NotifiedTopUp(result.clone()),
                );
                if is_transient_error(&result) {
                    state.blocks_notified.remove(&block_index);
                }
            });

            result
        }
    }
```

**File:** rs/nns/cmc/src/main.rs (L1925-1956)
```rust
async fn process_create_canister(
    controller: PrincipalId,
    from: AccountIdentifier,
    amount: Tokens,
    subnet_selection: Option<SubnetSelection>,
    settings: Option<CanisterSettings>,
) -> Result<CanisterId, NotifyError> {
    let cycles = tokens_to_cycles(amount)?;

    let sub = Subaccount::from(&controller);

    print(format!(
        "Creating canister with controller {controller} with {cycles} cycles.",
    ));

    // Create the canister. If this fails, refund. Either way,
    // return a result so that the notification cannot be retried.
    // If refund fails, we allow to retry.
    match do_create_canister(controller, cycles, subnet_selection, settings).await {
        Ok(canister_id) => {
            burn_and_log(sub, amount).await;
            Ok(canister_id)
        }
        Err(err) => {
            let refund_block = refund_icp(sub, from, amount, CREATE_CANISTER_REFUND_FEE).await?;
            Err(NotifyError::Refunded {
                reason: err,
                block_index: refund_block,
            })
        }
    }
}
```

**File:** rs/nns/cmc/src/main.rs (L1958-1983)
```rust
async fn process_mint_cycles(
    to_account: Account,
    amount: Tokens,
    deposit_memo: Option<Vec<u8>>,
    from: AccountIdentifier,
    sub: Subaccount,
) -> NotifyMintCyclesResult {
    let cycles = tokens_to_cycles(amount)?;
    match do_mint_cycles(to_account, cycles, deposit_memo).await {
        Ok(deposit_result) => {
            burn_and_log(sub, amount).await;
            Ok(NotifyMintCyclesSuccess {
                block_index: deposit_result.block_index,
                minted: cycles.into(),
                balance: deposit_result.balance,
            })
        }
        Err(err) => {
            let refund_block = refund_icp(sub, from, amount, MINT_CYCLES_REFUND_FEE).await?;
            Err(NotifyError::Refunded {
                reason: err,
                block_index: refund_block,
            })
        }
    }
}
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

**File:** rs/nns/cmc/src/main.rs (L2014-2048)
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
```
