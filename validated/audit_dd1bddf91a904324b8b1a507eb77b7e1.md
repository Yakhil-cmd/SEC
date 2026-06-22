### Title
Unchecked ICP Burn Return Value in Cycles Minting Canister Allows Conservation Violation - (`rs/nns/cmc/src/main.rs`)

---

### Summary

The `burn_and_log` function in the Cycles Minting Canister (CMC) swallows ledger transfer errors and unconditionally returns `()`. Its callers, `process_top_up` and `process_mint_cycles`, call `burn_and_log(...).await` without inspecting any result, then immediately return `Ok(cycles)` to the user. If the ICP ledger is transiently unavailable when the burn is attempted (e.g., during a ledger canister upgrade), cycles are minted and deposited to the target canister while the corresponding ICP is never burned, violating the ICP/cycles conservation invariant.

---

### Finding Description

In `rs/nns/cmc/src/main.rs`, the function `burn_and_log` (lines 2017–2048) is responsible for burning ICP from the CMC's subaccount after cycles have been successfully minted. The function makes a protobuf ledger call (`send_pb`) and matches on the result:

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

On failure, the error is only printed to the canister log. The function signature is `async fn burn_and_log(...) -> ()` — it returns nothing and propagates no error. [1](#0-0) 

Both callers invoke `burn_and_log` after a successful cycle deposit and immediately return success:

```rust
// process_top_up
Ok(()) => {
    burn_and_log(sub, amount).await;   // result is ()
    Ok(cycles)                          // success returned regardless
}

// process_mint_cycles
Ok(deposit_result) => {
    burn_and_log(sub, amount).await;   // result is ()
    Ok(NotifyMintCyclesSuccess { ... }) // success returned regardless
}
``` [2](#0-1) [3](#0-2) 

Once `notify_top_up` or `notify_mint_cycles` returns `Ok`, the block's status is permanently written as `NotifiedTopUp(Ok(cycles))` or `NotifiedMint(Ok(...))` in `blocks_notified`. This prevents any future retry of the burn for that block. [4](#0-3) 

The comment in the code acknowledges the intentional design but does not account for the conservation consequence:

> "Burning doesn't return errors - we don't want to reject the transaction notification because then it could be retried."

This reasoning prevents double-minting but simultaneously prevents the burn from ever being retried, leaving ICP permanently unburned in the CMC subaccount.

---

### Impact Explanation

**Vulnerability class**: Ledger conservation bug — unchecked transfer return value.

When the ICP ledger is transiently unavailable (e.g., during a canister upgrade, which is a routine NNS operation), the `send_pb` call in `burn_and_log` will fail with a reject. The CMC will:

1. Have already deposited cycles to the target canister (irreversible).
2. Fail to burn the ICP from its subaccount (silently).
3. Mark the block as permanently processed (`NotifiedTopUp`).
4. Return `Ok(cycles)` to the caller.

The net result is that ICP remains in the CMC's subaccount while cycles were created. The ICP/cycles conservation invariant — that every cycle minted corresponds to burned ICP — is violated. Over repeated occurrences, the total ICP supply is inflated relative to cycles in circulation. The unburned ICP is not recoverable through the normal notification path since the block is marked as already processed.

---

### Likelihood Explanation

The ICP ledger undergoes routine upgrades as part of NNS governance proposals. During an upgrade, the ledger canister is briefly unavailable (it stops accepting calls). Any `notify_top_up` or `notify_mint_cycles` call that reaches the burn step during this window will trigger the silent failure. An unprivileged user who sends ICP to the CMC and calls `notify_top_up` at the right moment (or whose call happens to be processed during a ledger upgrade) can trigger this condition without any special access. The window is short but the condition is deterministic and repeatable across every ledger upgrade.

---

### Recommendation

`burn_and_log` should propagate errors to its callers rather than swallowing them. The callers should handle burn failure by either:

1. **Retrying the burn** on the next heartbeat/timer tick while keeping the block in a `BurnPending` state, or
2. **Not marking the block as fully processed** until the burn succeeds, so the user can re-trigger the notification to complete the burn.

At minimum, the block status should distinguish between "cycles deposited, burn pending" and "fully finalized" to allow recovery.

---

### Proof of Concept

1. User transfers ICP to `AccountIdentifier::new(CYCLES_MINTING_CANISTER_ID, Some(subaccount_of_target_canister))` with `MEMO_TOP_UP_CANISTER`.
2. User calls `notify_top_up { block_index, canister_id }` on the CMC.
3. CMC calls `fetch_transaction` — succeeds, block verified.
4. CMC sets block status to `Processing`.
5. CMC calls `process_top_up` → `deposit_cycles(canister_id, cycles, ...)` — **succeeds**, cycles deposited to target canister.
6. CMC calls `burn_and_log(sub, amount).await` — the ICP ledger is mid-upgrade and rejects the call.
7. `burn_and_log` logs `"Burning of X ICPTs from subaccount Y failed with code Z: ..."` and returns `()`.
8. `process_top_up` returns `Ok(cycles)`.
9. CMC writes `blocks_notified[block_index] = NotifiedTopUp(Ok(cycles))` — **permanently**.
10. CMC returns `Ok(cycles)` to the user.

**Result**: Target canister received cycles. ICP was not burned. Block is permanently marked as processed. ICP remains in CMC subaccount. Conservation invariant violated. [5](#0-4) [6](#0-5)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1213-1225)
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

            result
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
