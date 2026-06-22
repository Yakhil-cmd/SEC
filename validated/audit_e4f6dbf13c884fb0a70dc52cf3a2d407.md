### Title
Unchecked Inter-Canister Call Result in `burn_and_log` Causes ICP to Become Permanently Stuck in CMC Subaccount - (File: rs/nns/cmc/src/main.rs)

### Summary
The `burn_and_log` function in the Cycles Minting Canister (CMC) makes an inter-canister call to the ICP ledger to burn ICP from a CMC subaccount after successful `notify_create_canister`, `notify_top_up`, and `notify_mint_cycles` operations. The result of this ledger call is never checked — errors are only logged. If the call fails, the ICP is permanently stuck in the CMC's subaccount with no recovery path, because the corresponding block is already marked as successfully processed and cannot be retried.

### Finding Description
In `rs/nns/cmc/src/main.rs`, the `burn_and_log` function is called after each successful payment-processing operation:

- `process_create_canister` (line 1945): `burn_and_log(sub, amount).await;`
- `process_top_up` (line 2001): `burn_and_log(sub, amount).await;`
- `process_mint_cycles` (line 1968): `burn_and_log(sub, amount).await;`

Inside `burn_and_log`, the ledger call is:

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

The error branch only logs the failure and returns `()`. The function signature is `async fn burn_and_log(...) -> ()` — there is no `Result` return type and no error propagation. The comment at line 2014–2016 explicitly acknowledges this:

> "Burning doesn't return errors - we don't want to reject the transaction notification because then it could be retried."

However, this design creates a permanent fund-lock condition: once the block is marked `NotifiedCreateCanister(Ok(...))`, `NotifiedTopUp(Ok(...))`, or `NotifiedMint(Ok(...))` in `blocks_notified`, any subsequent call with the same `block_index` returns the cached result immediately without re-attempting the burn. The ICP remains in the CMC's subaccount (e.g., `Subaccount::from(&controller)` or `Subaccount::from(&canister_id)`) indefinitely with no on-chain recovery mechanism. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

### Impact Explanation
**Ledger conservation bug**: ICP that should be burned (removed from supply) is instead permanently locked in a CMC subaccount. The ICP supply is not correctly reduced, and the locked ICP cannot be recovered by the user, the CMC, or any governance action without a canister upgrade. Over time, repeated ledger transient failures could accumulate non-trivial amounts of ICP in CMC subaccounts. The block deduplication map (`blocks_notified`) prevents any retry, making the loss permanent per-block.

### Likelihood Explanation
**Low-to-medium**: The ICP ledger is a system canister and is generally available, but transient failures are possible during ledger upgrades, subnet instability, or if the CMC's outgoing message queue is full. The `send_pb` call is a protobuf-encoded transfer with `fee: Tokens::ZERO` (minting burn), so insufficient-balance failures are unlikely, but call-level rejections (e.g., `SysTransient`, queue full) are realistic. Any user who successfully calls `notify_create_canister`, `notify_top_up`, or `notify_mint_cycles` during such a window triggers the condition.

### Recommendation
1. Return a `Result` from `burn_and_log` and propagate the error to the caller, allowing the block status to remain `Processing` so the user can retry.
2. Alternatively, store failed burn attempts in stable state and retry them in a heartbeat/timer, similar to how the CMC already retries other operations.
3. At minimum, emit a certified metric counter for failed burns so operators can detect and manually recover stuck ICP via governance upgrade.

### Proof of Concept
1. User sends ICP to the CMC's subaccount with `MEMO_CREATE_CANISTER` and calls `notify_create_canister`.
2. CMC fetches the transaction, sets block status to `Processing`, and calls `do_create_canister` — which succeeds.
3. CMC calls `burn_and_log(sub, amount).await` to burn the ICP from the subaccount.
4. The ICP ledger rejects the call with `SysTransient` (e.g., during a ledger upgrade window).
5. `burn_and_log` logs the error and returns `()` — no error is propagated.
6. CMC sets block status to `NotifiedCreateCanister(Ok(new_canister_id))`.
7. The ICP remains in `Subaccount::from(&controller)` of the CMC indefinitely.
8. Any retry of `notify_create_canister` with the same `block_index` returns the cached `Ok(new_canister_id)` immediately without re-attempting the burn.
9. The ICP is permanently stuck — neither burned nor refundable. [5](#0-4) [6](#0-5)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1405-1423)
```rust
    match maybe_early_result {
        Some(result) => result,
        None => {
            let result =
                process_create_canister(controller, from, amount, subnet_selection, settings).await;

            with_state_mut(|state| {
                state.blocks_notified.insert(
                    block_index,
                    NotificationStatus::NotifiedCreateCanister(result.clone()),
                );
                if is_transient_error(&result) {
                    state.blocks_notified.remove(&block_index);
                }
            });

            result
        }
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

**File:** rs/nns/cmc/src/main.rs (L1966-1982)
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
```

**File:** rs/nns/cmc/src/main.rs (L1999-2011)
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
