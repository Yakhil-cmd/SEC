### Title
Burn Result Silently Ignored in CMC Allows ICP to Remain Unburned After Cycles Minting — (File: rs/nns/cmc/src/main.rs)

---

### Summary

The `burn_and_log` function in the Cycles Minting Canister (CMC) intentionally discards the result of the ICP burn ledger call. When the burn fails due to a transient ledger error, cycles have already been minted and delivered to the user, but the corresponding ICP is never burned. This creates a ledger conservation discrepancy: ICP supply is not reduced as the protocol requires, and the unburned ICP is permanently stranded in the CMC's subaccount.

---

### Finding Description

In `rs/nns/cmc/src/main.rs`, the function `burn_and_log` (line 2017) is explicitly designed to swallow burn failures:

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
``` [1](#0-0) 

The function returns `()` regardless of whether the burn succeeded or failed. It is called unconditionally after every successful cycles-delivery operation:

- `process_create_canister` — line 1945: `burn_and_log(sub, amount).await;`
- `process_mint_cycles` — line 1968: `burn_and_log(sub, amount).await;`
- `process_top_up` — line 2001: `burn_and_log(sub, amount).await;` [2](#0-1) [3](#0-2) [4](#0-3) 

In all three paths, the cycles/canister delivery is committed to state **before** `burn_and_log` is called. The block is then recorded as `NotifiedTopUp(Ok(...))`, `NotifiedMint(Ok(...))`, or `NotifiedCreateCanister(Ok(...))`, permanently preventing any retry. [5](#0-4) 

---

### Impact Explanation

If the ICP ledger's `send_pb` call inside `burn_and_log` fails (e.g., transient reject, ledger upgrade in progress, output queue full):

1. The user has already received cycles or a new canister.
2. The ICP tokens remain in the CMC's per-user subaccount, unburned.
3. The block is permanently marked as processed — no retry is possible.
4. The ICP supply is not reduced as the protocol requires, violating the ICP/cycles conservation invariant.

The stranded ICP in the CMC's subaccount cannot be recovered by the user (no withdrawal path exists) and cannot be re-burned automatically (no retry mechanism). This is a **ledger conservation bug**: cycles are minted without the corresponding ICP destruction.

---

### Likelihood Explanation

The ICP ledger is an inter-canister call target. Transient failures are possible during:
- Ledger canister upgrades (the ledger is briefly unavailable).
- Subnet congestion causing output queue saturation.
- Any transient `TemporarilyUnavailable` reject from the ledger.

The entry path is fully unprivileged: any user who calls `notify_top_up`, `notify_create_canister`, or `notify_mint_cycles` with a valid ICP transfer block index can trigger this code path. The failure window is narrow (between cycles delivery and burn), but the consequence is permanent and unrecoverable.

---

### Recommendation

1. **Track failed burns**: Record burn failures in persistent state and retry them via a heartbeat or timer, using `created_at_time` deduplication on the ledger to prevent double-burns.
2. **Alternatively**: Before marking the block as fully processed, attempt the burn and only finalize the `NotifiedTopUp`/`NotifiedMint`/`NotifiedCreateCanister` status after the burn succeeds. If the burn fails transiently, leave the block in `Processing` state so the user can retry the notification.
3. At minimum, expose a metric or certified variable tracking the count and total amount of failed burns so operators can detect and manually remediate conservation failures.

---

### Proof of Concept

1. User sends 10 ICP to `CMC_subaccount(canister_id)` with `MEMO_TOP_UP_CANISTER`.
2. User calls `notify_top_up { block_index, canister_id }`.
3. CMC fetches the transaction, marks the block as `Processing`.
4. CMC calls `deposit_cycles(canister_id, cycles, ...)` — **succeeds**; cycles are delivered.
5. CMC calls `burn_and_log(sub, amount)`, which calls `send_pb` on the ICP ledger.
6. The ICP ledger is momentarily unavailable (e.g., mid-upgrade) — `send_pb` returns `Err`.
7. `burn_and_log` logs the error and returns `()`.
8. CMC records `NotifiedTopUp(Ok(cycles))` — block is permanently consumed.
9. **Result**: The target canister received cycles; the 10 ICP was never burned and sits stranded in the CMC's subaccount. The ICP supply is inflated relative to the cycles supply by 10 ICP. [6](#0-5)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1299-1316)
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
```

**File:** rs/nns/cmc/src/main.rs (L1940-1956)
```rust
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

**File:** rs/nns/cmc/src/main.rs (L1966-1983)
```rust
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

**File:** rs/nns/cmc/src/main.rs (L1999-2012)
```rust
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
