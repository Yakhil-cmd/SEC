### Title
Silently Ignored Ledger Burn Result in CMC Allows ICP Conservation Failure - (File: `rs/nns/cmc/src/main.rs`)

### Summary
The Cycles Minting Canister (CMC) deliberately ignores the result of the ICP burn call after successfully minting or depositing cycles. If the ledger's `send_pb` call fails transiently (e.g., during a ledger upgrade or temporary unavailability), cycles are minted/deposited to the user while the corresponding ICP is never burned, breaking the ICP-to-cycles conservation invariant.

### Finding Description

In `rs/nns/cmc/src/main.rs`, the function `burn_and_log` is called after cycles have already been deposited or minted. It makes an inter-canister call to the ICP ledger to burn the ICP from the CMC's subaccount, but explicitly discards the error:

```rust
async fn burn_and_log(from_subaccount: Subaccount, amount: Tokens) {
    // ...
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

The comment above the function explicitly states the design intent:

> "Burning doesn't return errors - we don't want to reject the transaction notification because then it could be retried."

This function is called from both `process_top_up` and `process_mint_cycles` after cycles have already been committed: [2](#0-1) [3](#0-2) 

The call sequence is:
1. `notify_top_up` / `notify_mint_cycles` → `process_top_up` / `process_mint_cycles`
2. Cycles are deposited to the target canister / minted to the cycles ledger (committed, irreversible)
3. `burn_and_log` is called — if the ledger call fails, the error is only logged, never propagated

This is the IC analog of the ERC20 unsafe transfer pattern: an external call whose failure is silently swallowed, with the calling canister proceeding as if the operation succeeded.

### Impact Explanation

If `burn_and_log` fails (e.g., the ICP ledger is temporarily unavailable during an upgrade window), the ICP deposited in the CMC's subaccount is never burned. Cycles have already been minted and delivered. The result is:

- **ICP supply inflation**: ICP that should have been destroyed remains in circulation (locked in the CMC subaccount, inaccessible to the user but not burned).
- **Broken conservation invariant**: The total cycles minted no longer corresponds to the total ICP burned, violating the economic model of the IC.
- The ICP stuck in the CMC subaccount cannot be recovered by the user (they already received cycles), and there is no retry mechanism for the burn.

### Likelihood Explanation

The ICP ledger is a trusted NNS canister. The most realistic failure window is during a ledger canister upgrade, when the ledger is briefly stopped. Any `notify_top_up` or `notify_mint_cycles` call that completes its cycles deposit step during this window will silently fail to burn the ICP. An unprivileged user can trigger this by:

1. Sending ICP to the CMC's subaccount before a known ledger upgrade
2. Calling `notify_top_up` or `notify_mint_cycles` timed to complete the cycles deposit while the ledger is upgrading

NNS upgrade proposals are public and their execution timing is observable on-chain, making this window predictable. The likelihood is low under normal conditions but non-negligible around upgrade events.

### Recommendation

After cycles are deposited/minted, the burn should be retried or tracked in persistent state so it can be completed later. A minimal fix is to record failed burns in the CMC's state (similar to how `blocks_notified` tracks notification status) and retry them in a heartbeat. Alternatively, the burn should be attempted before cycles are deposited, with the deposit only proceeding on burn success — though this changes the atomicity model.

### Proof of Concept

1. Observe a pending NNS proposal to upgrade the ICP ledger canister.
2. Send ICP to `CMC_CANISTER_ID` subaccount derived from a target canister ID with memo `MEMO_TOP_UP_CANISTER`.
3. Time a call to `notify_top_up` so that `deposit_cycles` (the management canister call) completes just before the ledger upgrade stops the ledger.
4. `burn_and_log` calls `send_pb` on the stopped ledger → receives a reject → logs the error and returns `()`.
5. The caller receives `Ok(cycles)` from `notify_top_up`. Cycles are in the target canister. The ICP is not burned.
6. After the ledger restarts, the ICP remains in the CMC subaccount with no mechanism to burn it retroactively. [1](#0-0) [4](#0-3)

### Citations

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
