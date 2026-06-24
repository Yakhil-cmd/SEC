### Title
Unvalidated Ledger Burn Return in CMC `burn_and_log` Silently Drops Fee Burns - (File: rs/nns/cmc/src/main.rs)

### Summary
The Cycles Minting Canister (CMC) contains a function `burn_and_log` that performs an ICP ledger transfer (burn) but deliberately discards the `Result`, returning `()` regardless of success or failure. Its sole caller, `refund_icp`, proceeds to return `Ok(...)` to the upstream notification handler without knowing whether the burn actually occurred. This is the direct IC analog of the ERC20 `transfer` without `safeTransfer`: a token-moving call whose failure is silently swallowed, leaving the ledger in a state inconsistent with protocol expectations.

### Finding Description

`burn_and_log` in `rs/nns/cmc/src/main.rs` is declared `async fn burn_and_log(...) -> ()`. It calls the ICP ledger via `call_protobuf` to send tokens to the minting account (a burn), matches on the `CallResult`, and logs — but never surfaces the error to its caller: [1](#0-0) 

The function signature returns `()`, so the caller has no mechanism to detect failure: [2](#0-1) 

`refund_icp` calls `burn_and_log(from_subaccount, burned).await` and then unconditionally returns `Ok(refund_block_index)`: [3](#0-2) 

The comment in `burn_and_log` explicitly acknowledges the design: *"Burning doesn't return errors — we don't want to reject the transaction notification because then it could be retried."* This intentional suppression of the error is the root cause: the burn call result is not propagated, so a failed burn is indistinguishable from a successful one at the call site.

### Impact Explanation

When `burn_and_log` fails (e.g., the ICP ledger is temporarily unavailable or rejects the call), the fee ICP that should be destroyed remains in the CMC's subaccount. The ICP ledger's total supply is not reduced as the protocol requires. Over repeated failures, unburned ICP accumulates in CMC subaccounts, creating a persistent ledger conservation discrepancy: the on-chain total supply exceeds what the protocol's accounting model expects. Because the CMC has no retry or reconciliation logic for failed burns, the discrepancy is permanent unless manually corrected via an upgrade.

### Likelihood Explanation

The ICP ledger canister can transiently reject calls (e.g., during upgrades, under heavy load, or if the ledger traps). Any user who triggers a `notify_create_canister` or `notify_top_up` flow that results in a refund path during such a window will cause a silent burn failure. The entry path requires no privileged access — any principal can send ICP to the CMC and call the notify endpoints. The ledger unavailability window is the only external precondition, making this a realistic low-frequency but persistent ledger conservation bug.

### Recommendation

Change `burn_and_log` to return `Result<(), NotifyError>` (or a suitable error type) and propagate the ledger call result:

```rust
async fn burn_and_log(
    from_subaccount: Subaccount,
    amount: Tokens,
) -> Result<(), (i32, String)> {
    // ...
    let res: CallResult<BlockIndex> =
        call_protobuf(ledger_canister_id, "send_pb", send_args).await;
    match res {
        Ok(block) => { print(format!("{msg} done in block {block}.")); Ok(()) }
        Err((code, err)) => {
            print(format!("{msg} failed with code {code}: {err:?}"));
            Err((code as i32, err))
        }
    }
}
```

Then in `refund_icp`, handle the error — either by returning an error to the caller, scheduling a retry, or recording the failed burn for later reconciliation. The comment's concern about retryability is valid, but the correct fix is idempotent retry logic (e.g., using `created_at_time` deduplication on the ledger), not silent discard.

### Proof of Concept

1. User sends 2 ICP to the CMC's subaccount for their principal.
2. User calls `notify_create_canister`; the CMC determines the subnet has no capacity and decides to refund.
3. `refund_icp` is called: it successfully refunds ~1 ICP to the user (the main transfer succeeds), then calls `burn_and_log` to destroy the ~1 ICP fee.
4. At this moment, the ICP ledger is mid-upgrade and rejects the call with a transient error.
5. `burn_and_log` logs `"Burning of 1 ICP failed with code -1: ..."` and returns `()`.
6. `refund_icp` returns `Ok(Some(block_index))` — success — to the notification handler.
7. The ~1 ICP fee remains in the CMC's subaccount. The ICP ledger total supply is 1 ICP higher than the protocol's accounting model expects.
8. No retry or reconciliation occurs; the discrepancy is permanent. [4](#0-3) [5](#0-4)

### Citations

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

**File:** rs/nns/cmc/src/main.rs (L2055-2108)
```rust
async fn refund_icp(
    from_subaccount: Subaccount,
    to: AccountIdentifier,
    amount: Tokens,
    extra_fee: Tokens,
) -> Result<Option<BlockIndex>, NotifyError> {
    let ledger_canister_id = with_state(|state| state.ledger_canister_id);
    let mut refund_block_index = None;

    let mut burned = amount;
    let mut refunded = Tokens::ZERO;
    if let Ok(to_refund) = amount
        .checked_sub(&DEFAULT_TRANSFER_FEE)
        .ok_or("Underflow in subtracting the fee from amount")
        .and_then(|x| {
            x.checked_sub(&extra_fee)
                .ok_or("Underflow in subtracting the extra fee from the amount")
        })
        && to_refund > Tokens::ZERO
    {
        burned = extra_fee;
        refunded = to_refund;
    }

    if refunded > Tokens::ZERO {
        let send_args = SendArgs {
            memo: Memo::default(),
            amount: refunded,
            fee: DEFAULT_TRANSFER_FEE,
            from_subaccount: Some(from_subaccount),
            to,
            created_at_time: None,
        };
        let send_res: CallResult<BlockIndex> =
            call_protobuf(ledger_canister_id, "send_pb", send_args).await;
        let block = send_res.map_err(|(code, err)| {
            let code = code as i32;
            NotifyError::Other {
                error_code: NotifyErrorCode::RefundFailed as u64,
                error_message: format!("Refund to {to} failed with code {code}: {err}"),
            }
        })?;

        print(format!("Refund to {to} done in block {block}."));

        refund_block_index = Some(block);
    }

    if burned > Tokens::ZERO {
        burn_and_log(from_subaccount, burned).await;
    }

    Ok(refund_block_index)
}
```
