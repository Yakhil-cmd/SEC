### Title
Silent Burn Failure in Cycles Minting Canister Breaks ICP Supply Conservation - (File: `rs/nns/cmc/src/main.rs`)

### Summary
The `burn_and_log` function in the Cycles Minting Canister (CMC) silently discards ledger transfer errors when burning ICP after successfully minting cycles or creating canisters. If the ICP ledger is temporarily unavailable (e.g., during a routine upgrade), cycles are minted or canisters are created without the corresponding ICP being burned, permanently breaking ICP supply conservation.

### Finding Description
In `rs/nns/cmc/src/main.rs`, the `burn_and_log` function is called after every successful operation in `process_create_canister`, `process_top_up`, and `process_mint_cycles`: [1](#0-0) 

The function issues a `send_pb` call to the ICP ledger to burn the ICP from CMC's subaccount. When this call fails, the function only logs the error and returns `()` — no error is propagated, no retry is scheduled, and no corrective state is recorded: [2](#0-1) 

The callers (`process_create_canister`, `process_top_up`, `process_mint_cycles`) do not check the return value of `burn_and_log` because it returns `()`: [3](#0-2) [4](#0-3) 

By the time `burn_and_log` is called, the notification block index has already been marked as `Processing` (and will be finalized as processed), so the user cannot retry the notification to trigger a second burn attempt. The ICP remains permanently stranded in CMC's subaccount.

The design comment explicitly acknowledges this: *"Burning doesn't return errors - we don't want to reject the transaction notification because then it could be retried."* This trades retry-safety for supply conservation correctness.

### Impact Explanation
- **Ledger conservation bug**: Cycles are minted (or canisters created) without the corresponding ICP being burned. The ICP supply is not reduced as the protocol requires.
- **Stranded ICP**: The unburned ICP accumulates in CMC's subaccount with no protocol-level mechanism to recover or re-burn it.
- **Supply inflation**: Each failed burn permanently inflates the effective ICP supply relative to the cycles minted, violating the ICP/cycles economic invariant.
- The stranded ICP is not accessible to the user (no refund path) and not usable by CMC for any other purpose — it is effectively lost from the burn accounting.

### Likelihood Explanation
The ICP ledger undergoes routine upgrades on the NNS subnet. During an upgrade window (seconds to minutes), the ledger is temporarily unavailable. Any unprivileged user who:
1. Sends ICP to CMC's subaccount, and
2. Calls `notify_create_canister`, `notify_top_up`, or `notify_mint_cycles` while the ledger is upgrading

will trigger this path. No special privileges, keys, or coordination are required. The attacker does not need to predict the upgrade window precisely — the CMC processes notifications asynchronously and the burn call happens after the main operation succeeds.

### Recommendation
Track failed burns in persistent CMC state and retry them on a timer, analogous to how the ckBTC/ckETH minters handle failed mint retries. Alternatively, record a `PendingBurn` entry in `blocks_notified` state and process it on subsequent timer ticks, ensuring every successful cycles-minting operation is eventually matched by a confirmed ICP burn.

### Proof of Concept
1. User sends 10 ICP to `CMC_CANISTER_ID` subaccount with `MEMO_TOP_UP_CANISTER`.
2. User calls `notify_top_up` on CMC.
3. CMC calls `deposit_cycles` → succeeds, cycles are deposited to the target canister.
4. CMC calls `burn_and_log(sub, amount)` → the ICP ledger is mid-upgrade and returns a reject.
5. `burn_and_log` logs the error and returns `()`.
6. `process_top_up` returns `Ok(cycles)` to the caller.
7. The block index is marked as `NotificationProcessed` in `blocks_notified`.
8. Result: 10 ICP worth of cycles were minted; 10 ICP remain in CMC's subaccount unburned; the notification cannot be retried. [5](#0-4) [6](#0-5)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1943-1956)
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
