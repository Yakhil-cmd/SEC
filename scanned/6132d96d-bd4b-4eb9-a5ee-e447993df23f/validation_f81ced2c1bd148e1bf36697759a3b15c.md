### Title
Silently Ignored Ledger Burn Failure in CMC Allows ICP Supply Inflation Without Burning - (`rs/nns/cmc/src/main.rs`)

### Summary

The Cycles Minting Canister (CMC) intentionally discards the return value of the ICP ledger burn call inside `burn_and_log`. After cycles are successfully minted and deposited to a target canister, the CMC attempts to burn the corresponding ICP from its own subaccount. If that ledger call fails for any reason, the failure is only logged; the notification is still recorded as fully successful. The ICP remains permanently stranded in the CMC's subaccount, the ICP total supply is not reduced, and the block index is permanently marked as processed so no retry is possible.

### Finding Description

After a successful `deposit_cycles` (or `do_create_canister` / `do_mint_cycles`) call, the CMC calls `burn_and_log` to destroy the ICP that was exchanged for cycles. [1](#0-0) 

The function explicitly documents that it swallows errors:

```
/// Attempt to burn the funds.
/// Burning doesn't return errors - we don't want to reject the transaction
/// notification because then it could be retried.
``` [2](#0-1) 

The ledger `send_pb` result is matched only for logging; no error is propagated:

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

`burn_and_log` returns `()` in both branches. The callers (`process_top_up`, `process_create_canister`, `process_mint_cycles`) then return `Ok(...)` unconditionally: [3](#0-2) [4](#0-3) [5](#0-4) 

The `notify_top_up` / `notify_create_canister` / `notify_mint_cycles` handlers then permanently record the block as `NotifiedTopUp(Ok(...))` / `NotifiedCreateCanister(Ok(...))` / `NotifiedMint(Ok(...))`: [6](#0-5) 

Once the block is recorded as successfully processed, no retry path exists. The ICP sitting in the CMC's subaccount is permanently unburnable through the normal notification flow.

### Impact Explanation

**Ledger conservation bug / ICP supply inflation.** When `burn_and_log` fails:

1. Cycles have already been minted and deposited to the target canister — irreversible.
2. The ICP in the CMC's subaccount is **not** burned — the ICP total supply is not reduced.
3. The block index is permanently marked as processed — the user cannot retry to trigger the burn.
4. The ICP is permanently stranded in the CMC's subaccount with no recovery path for the user.

The ICP ledger's total supply diverges from the expected value (cycles minted without corresponding ICP destruction). This violates the invariant that every cycle minted corresponds to burned ICP.

### Likelihood Explanation

The trigger condition is the ICP ledger being temporarily unreachable between the `deposit_cycles` call and the `burn_and_log` call within the same async execution. This can occur during:

- **Ledger canister upgrades** (routine NNS governance events, happening every few weeks).
- **Transient canister queue overflow** under high ledger load — no privileged access required.
- **Transient inter-subnet messaging delays** on the NNS subnet.

The window is narrow (a single async await point), but the condition is not exotic. Ledger upgrades are frequent and predictable. An observer who knows an upgrade is imminent can time a `notify_top_up` call to land in this window. The attacker does not lose funds (they receive their cycles), so there is no disincentive to attempt this repeatedly.

### Recommendation

`burn_and_log` should propagate its failure so that the notification result reflects the burn outcome. One approach:

- Change `burn_and_log` to return `Result<BlockIndex, NotifyError>`.
- If the burn fails, do **not** record the block as `NotifiedTopUp(Ok(...))` — leave it as `Processing` or remove it so the user can retry.
- Alternatively, record a distinct `NotificationStatus::BurnPending` state and retry the burn in a heartbeat, before finalising the notification as complete.

The current comment "we don't want to reject the transaction notification because then it could be retried" conflates two concerns: retrying the cycle-minting step (dangerous) vs. retrying the burn step (safe and necessary). These should be separated.

### Proof of Concept

1. User sends ICP to the CMC's top-up subaccount for canister `C` with memo `MEMO_TOP_UP_CANISTER`.
2. User calls `notify_top_up { block_index, canister_id: C }`.
3. CMC calls `deposit_cycles` → succeeds; cycles are deposited to `C`.
4. CMC calls `burn_and_log` → the ICP ledger is temporarily unavailable (e.g., mid-upgrade); the `send_pb` call returns an error.
5. `burn_and_log` logs the error and returns `()`.
6. `process_top_up` returns `Ok(cycles)`.
7. `notify_top_up` stores `NotifiedTopUp(Ok(cycles))` for `block_index`.
8. **Result**: User received cycles; ICP was not burned; block is permanently marked as processed; ICP supply is inflated by `amount`. [7](#0-6) [8](#0-7)

### Citations

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
