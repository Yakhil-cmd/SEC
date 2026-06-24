### Title
Silent ICP Burn Failure After Cycles Minting Breaks Conservation Invariant — (`File: rs/nns/cmc/src/main.rs`)

---

### Summary

The Cycles Minting Canister (CMC) calls `burn_and_log` to destroy the ICP after successfully minting cycles, topping up a canister, or creating a canister. `burn_and_log` is explicitly designed to swallow all errors from the ICP ledger `send_pb` call and return `()` unconditionally. If the burn fails, cycles have already been irrevocably minted/deposited while the corresponding ICP remains unburned in the CMC's subaccount, permanently breaking the ICP/cycles conservation invariant.

---

### Finding Description

After a successful cycles operation, the CMC calls `burn_and_log` in three places:

```
process_create_canister  → burn_and_log(sub, amount).await;   // line 1945
process_mint_cycles      → burn_and_log(sub, amount).await;   // line 1968
process_top_up           → burn_and_log(sub, amount).await;   // line 2001
```

The function itself:

```rust
// rs/nns/cmc/src/main.rs  lines 2014-2049
/// Attempt to burn the funds.
/// Burning doesn't return errors - we don't want to reject the transaction
/// notification because then it could be retried.
async fn burn_and_log(from_subaccount: Subaccount, amount: Tokens) {
    // ...
    let res: CallResult<BlockIndex> =
        call_protobuf(ledger_canister_id, "send_pb", send_args).await;

    match res {
        Ok(block) => print(format!("{msg} done in block {block}.")),
        Err((code, err)) => {
            let code = code as i32;
            print(format!("{msg} failed with code {code}: {err:?}"))
        }
    }
}
```

The `Err` arm only logs; it does not propagate the failure, does not schedule a retry, and does not record the failed burn anywhere in persistent state. The notification entry is already written as `NotifiedTopUp(Ok(...))` / `NotifiedMint(Ok(...))` / `NotifiedCreateCanister(Ok(...))` before `burn_and_log` is awaited, so the notification can never be replayed. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

---

### Impact Explanation

**Ledger conservation bug.** The ICP protocol invariant is: every batch of cycles minted corresponds to an equal-value ICP burn. If `burn_and_log` fails:

- Cycles are already deposited to the target canister or cycles-ledger account (irreversible).
- The ICP remains in the CMC's per-principal/per-canister subaccount, unburned.
- The notification is permanently marked as processed; it cannot be retried by the user.
- Total ICP supply is not reduced, while total cycles supply has increased — the conservation invariant is silently violated.
- The stranded ICP in the CMC subaccount is effectively locked: there is no recovery path exposed to users or operators. [5](#0-4) [6](#0-5) 

---

### Likelihood Explanation

**Low-to-medium.** The ICP ledger (`ryjl3-tyaaa-aaaaa-aaaba-cai`) is a high-availability NNS canister, but it can be transiently unavailable during:

- Canister upgrades (the ledger is upgraded periodically).
- Subnet-level message queue saturation.
- Archiving operations that temporarily block the ledger.

Any unprivileged user who calls `notify_top_up`, `notify_mint_cycles`, or `notify_create_canister` during such a window will trigger the path. The user does not need to do anything special — the race is purely environmental. Because the notification is immediately finalized as successful before `burn_and_log` is awaited, even a single transient ledger rejection is sufficient to permanently strand the ICP. [7](#0-6) [8](#0-7) 

---

### Recommendation

1. **Track failed burns in persistent state.** Add a `failed_burns: Vec<(Subaccount, Tokens)>` field to `State`. On burn failure, push the entry instead of only logging.
2. **Retry in a heartbeat.** A `canister_heartbeat` or timer task can drain `failed_burns`, retrying each `send_pb` call until it succeeds.
3. **Alternatively, burn before finalizing the notification.** Reorder the sequence so the ICP burn is attempted (and confirmed) before the notification status is written as `Ok`. If the burn fails, leave the notification as `Processing` so the user can retry.
4. **At minimum**, expose a metric counter for failed burns so operators can detect and manually intervene.

---

### Proof of Concept

1. User sends N ICP to CMC subaccount with `MEMO_TOP_UP_CANISTER`.
2. User calls `notify_top_up { block_index, canister_id }`.
3. CMC fetches the transaction, marks the block as `Processing`, calls `process_top_up`.
4. `deposit_cycles` succeeds — N cycles are deposited to `canister_id`.
5. CMC writes `NotifiedTopUp(Ok(cycles))` to `blocks_notified` — notification is now permanently finalized.
6. CMC calls `burn_and_log(sub, amount).await`.
7. The ICP ledger is transiently unavailable (e.g., mid-upgrade); `call_protobuf` returns `Err(...)`.
8. `burn_and_log` logs the error and returns `()`.
9. `notify_top_up` returns `Ok(cycles)` to the user.

**Result:** The user received N cycles. The N ICP remain in the CMC's subaccount for `canister_id`, unburned. The ICP/cycles conservation invariant is broken. The notification cannot be retried (it is already `NotifiedTopUp(Ok(...))`). [9](#0-8) [10](#0-9)

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

**File:** rs/nns/cmc/src/main.rs (L1299-1317)
```rust
    match maybe_early_result {
        Some(result) => result,
        None => {
            let result =
                process_mint_cycles(to_account, amount, deposit_memo, from, subaccount).await;

            with_state_mut(|state| {
                state.blocks_notified.insert(
                    block_index,
                    NotificationStatus::NotifiedMint(result.clone()),
                );
                if is_transient_error(&result) {
                    state.blocks_notified.remove(&block_index);
                }
            });

            result
        }
    }
```

**File:** rs/nns/cmc/src/main.rs (L1943-1946)
```rust
    match do_create_canister(controller, cycles, subnet_selection, settings).await {
        Ok(canister_id) => {
            burn_and_log(sub, amount).await;
            Ok(canister_id)
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
