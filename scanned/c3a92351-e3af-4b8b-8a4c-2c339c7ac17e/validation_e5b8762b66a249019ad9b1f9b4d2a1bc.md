### Title
Unverified ICP Burn in Cycles Minting Canister Allows ICP Supply Inflation - (File: rs/nns/cmc/src/main.rs)

### Summary
The `burn_and_log` function in the Cycles Minting Canister (CMC) silently ignores ledger burn failures. After successfully minting cycles or creating a canister, the CMC attempts to burn the corresponding ICP from its subaccount. If this burn fails, the notification is still marked as complete and cannot be retried, leaving ICP permanently un-burned while cycles have already been issued — inflating the ICP supply relative to the cycles backing it.

### Finding Description

The `burn_and_log` function is called after every successful cycles-minting operation in the CMC:

- `process_create_canister` → calls `burn_and_log` after `do_create_canister` succeeds
- `process_mint_cycles` → calls `burn_and_log` after `do_mint_cycles` succeeds  
- `process_top_up` → calls `burn_and_log` after `deposit_cycles` succeeds

The function itself explicitly discards the burn result:

```rust
// Attempt to burn the funds.
// Burning doesn't return errors - we don't want to reject the transaction
// notification because then it could be retried.
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

The function returns `()` in both the success and failure branches. The callers do not check whether the burn succeeded:

```rust
match do_create_canister(controller, cycles, subnet_selection, settings).await {
    Ok(canister_id) => {
        burn_and_log(sub, amount).await;  // result discarded
        Ok(canister_id)
    }
    ...
}
``` [2](#0-1) 

The same pattern applies in `process_mint_cycles` and `process_top_up`: [3](#0-2) [4](#0-3) 

After `burn_and_log` returns (regardless of success), the calling notification handler stores the result as `NotifiedCreateCanister`, `NotifiedMint`, or `NotifiedTopUp` in `blocks_notified`, permanently preventing any retry: [5](#0-4) 

### Impact Explanation

**Ledger conservation bug / ICP supply inflation.** The ICP ledger's invariant requires that every batch of cycles minted corresponds to an equal-value ICP burn. When `burn_and_log` fails silently:

1. Cycles have already been deposited to the target canister or minted to the cycles ledger (irreversible).
2. The ICP remains in the CMC's subaccount — it is never burned.
3. The block notification is permanently marked as processed — the user cannot retry, and the CMC has no recovery path.
4. The net effect is that ICP supply is inflated: cycles exist without a corresponding ICP burn backing them.

Over time, repeated failures accumulate un-burned ICP in the CMC's subaccounts, breaking the ICP↔cycles conservation invariant that underpins the economic model of the IC.

**Impact: High** — ICP supply inflation, permanent loss of the burn, no recovery mechanism.

### Likelihood Explanation

**Likelihood: Low.** The ICP ledger must be temporarily unavailable (e.g., during an upgrade, under heavy load causing `TxThrottled`, or a transient reject) at the precise moment `burn_and_log` is called — which is immediately after the cycles operation succeeds. This is a narrow window but is a realistic scenario during routine ledger upgrades on the NNS subnet. Any user who submits a `notify_top_up`, `notify_create_canister`, or `notify_mint_cycles` call during such a window triggers the condition without any special privilege.

### Recommendation

`burn_and_log` should propagate its error back to the callers. The callers should treat a burn failure as a transient error (similar to how `refund_icp` failures are handled) and clear the `blocks_notified` entry so the notification can be retried:

```rust
async fn burn_and_log(from_subaccount: Subaccount, amount: Tokens) -> Result<BlockIndex, String> {
    ...
    call_protobuf(ledger_canister_id, "send_pb", send_args)
        .await
        .map_err(|(code, err)| format!("{msg} failed with code {code}: {err:?}"))
}
```

In `process_create_canister`, `process_mint_cycles`, and `process_top_up`, if `burn_and_log` returns an error, the notification status should be cleared (not stored as `NotifiedXxx`) so the user can retry. This mirrors the existing pattern used for `refund_icp` failures, where `is_transient_error` causes the block status to be removed: [6](#0-5) 

### Proof of Concept

1. User sends ICP to CMC with `MEMO_TOP_UP_CANISTER` and calls `notify_top_up`.
2. CMC successfully calls `deposit_cycles` — cycles are deposited to the target canister.
3. CMC calls `burn_and_log`. At this moment, the ICP ledger is being upgraded (or returns `TxThrottled`). The `call_protobuf` call returns `Err(...)`.
4. `burn_and_log` logs the error and returns `()`.
5. `process_top_up` returns `Ok(cycles)`.
6. The notification handler stores `NotificationStatus::NotifiedTopUp(Ok(cycles))` for this block index.
7. The ICP remains in the CMC's subaccount. The user cannot retry (block is marked processed). The cycles were already deposited. ICP supply is inflated by `amount`.

### Citations

**File:** rs/nns/cmc/src/main.rs (L1302-1316)
```rust
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

**File:** rs/nns/cmc/src/main.rs (L1943-1955)
```rust
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
