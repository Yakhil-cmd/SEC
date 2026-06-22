### Title
ICP Burn Silently Ignored After Successful Cycles Minting, Breaking Conservation Invariant - (File: `rs/nns/cmc/src/main.rs`)

### Summary
The `burn_and_log` function in the Cycles Minting Canister (CMC) is explicitly designed to swallow all errors from the ICP ledger burn that must follow a successful cycles mint or canister creation. If the burn call fails for any reason, cycles have already been deposited to the caller, the ICP remains unburned in the CMC's subaccount, and the notification is permanently marked as complete — preventing any retry. This breaks the ICP/cycles conservation invariant: cycles are created without the corresponding ICP being destroyed.

### Finding Description

In `rs/nns/cmc/src/main.rs`, three top-level processing functions call `burn_and_log` **after** the irreversible action (cycles deposit or canister creation) has already succeeded:

- `process_top_up` (line 1999–2001): calls `deposit_cycles`, then `burn_and_log`
- `process_mint_cycles` (line 1966–1968): calls `do_mint_cycles`, then `burn_and_log`
- `process_create_canister` (line 1943–1945): calls `do_create_canister`, then `burn_and_log` [1](#0-0) [2](#0-1) [3](#0-2) 

The `burn_and_log` function is explicitly annotated with the comment: *"Burning doesn't return errors — we don't want to reject the transaction notification because then it could be retried."* It catches all ledger call errors and logs them, returning `()` regardless of outcome: [4](#0-3) 

Specifically, the burn silently no-ops if:
1. `minting_account_id` is `None` (line 2020–2023)
2. `amount < DEFAULT_TRANSFER_FEE` (line 2027–2030)
3. The `send_pb` call to the ICP ledger returns any `Err` (line 2044–2047)

After `burn_and_log` returns (whether or not the burn succeeded), the caller stores the result as a terminal `NotificationStatus` (`NotifiedTopUp`, `NotifiedMint`, or `NotifiedCreateCanister`), permanently closing the notification: [5](#0-4) 

### Impact Explanation

When `burn_and_log` fails silently:
- The user has already received cycles (deposited to their canister or cycles-ledger account).
- The ICP is **not** burned — it remains in the CMC's subaccount keyed by the user's principal/canister ID.
- The notification is permanently marked complete; the user cannot retry.
- The ICP/cycles conservation invariant is violated: new cycles exist without the corresponding ICP being destroyed, inflating the effective cycles supply relative to the ICP supply.

This is a **ledger conservation bug** directly analogous to the reported Solidity issue: a token transfer (the burn) can fail silently, and state (the notification status) is updated as if it succeeded.

### Likelihood Explanation

The ICP ledger is a production canister on the NNS subnet. Transient inter-canister call failures (reject codes from the subnet, ledger temporarily stopping for upgrade, message queue full) are realistic and have occurred historically on mainnet. An unprivileged user who happens to call `notify_top_up` or `notify_mint_cycles` during such a window will receive cycles while the ICP burn is silently dropped. No privileged access, key compromise, or majority attack is required — only a timing coincidence with a ledger transient error.

### Recommendation

1. **Do not silently discard burn failures.** If `burn_and_log` fails, the notification should either remain in `Processing` state (allowing retry) or be stored with a distinct `BurnFailed` status that a privileged operator can later resolve.
2. **Separate the burn from the success response.** The burn should be attempted before the notification is finalized, or the notification should only be finalized after the burn is confirmed.
3. **Add an invariant monitor** (e.g., in `heartbeat`) that compares the sum of all CMC subaccount balances against expected zero, alerting on any residual ICP that was not burned.

### Proof of Concept

1. User sends `N` ICP to the CMC's subaccount (keyed by their canister ID) with memo `MEMO_TOP_UP_CANISTER`.
2. User calls `notify_top_up { block_index, canister_id }`.
3. CMC fetches the transaction, sets `blocks_notified[block_index] = Processing`, and calls `process_top_up`.
4. `deposit_cycles` succeeds — the target canister receives cycles.
5. `burn_and_log` is called. At this moment, the ICP ledger is transiently unavailable (e.g., mid-upgrade). The `call_protobuf` call returns `Err(...)`. `burn_and_log` logs the error and returns `()`.
6. `process_top_up` returns `Ok(cycles)`. The CMC stores `NotifiedTopUp(Ok(cycles))`.
7. The user's canister has received cycles. The `N` ICP remains unburned in the CMC's subaccount. The notification is permanently closed.
8. The ICP/cycles conservation invariant is broken: `N` ICP was not destroyed but `N * rate` cycles were created. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1305-1312)
```rust
            with_state_mut(|state| {
                state.blocks_notified.insert(
                    block_index,
                    NotificationStatus::NotifiedMint(result.clone()),
                );
                if is_transient_error(&result) {
                    state.blocks_notified.remove(&block_index);
                }
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
