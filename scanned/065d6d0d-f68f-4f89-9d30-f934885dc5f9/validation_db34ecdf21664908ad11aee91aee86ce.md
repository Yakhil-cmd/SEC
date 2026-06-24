### Title
Silent ICP Burn Failure After Successful Cycles Minting Causes Ledger Conservation Bug - (File: `rs/nns/cmc/src/main.rs`)

### Summary

The Cycles Minting Canister (CMC) deliberately ignores errors from the ICP burn operation in `burn_and_log`. After successfully minting cycles (or creating a canister), the CMC calls `burn_and_log` to destroy the ICP held in its subaccount. If this burn fails for any reason, the error is silently swallowed — only a log message is printed — while the notification is already permanently recorded as successful. The ICP remains in the CMC subaccount indefinitely with no recovery path, inflating the effective ICP supply relative to what the protocol intends.

### Finding Description

In `rs/nns/cmc/src/main.rs`, three flows — `process_top_up`, `process_create_canister`, and `process_mint_cycles` — follow the same pattern:

1. Mint cycles / create canister / deposit to cycles ledger (the primary action).
2. On success, call `burn_and_log` to destroy the ICP from the CMC subaccount. [1](#0-0) [2](#0-1) [3](#0-2) 

`burn_and_log` is explicitly designed to never propagate errors: [4](#0-3) 

When the ledger call at line 2040 fails (e.g., transient ledger unavailability, queue full, or any reject), the `Err` arm at line 2044 only prints a log message and returns `()`. The function signature returns no `Result`, so callers cannot observe the failure.

By the time `burn_and_log` is called, the notification has already been committed to `blocks_notified` as `NotifiedTopUp(Ok(...))` / `NotifiedCreateCanister(Ok(...))` / `NotifiedMint(Ok(...))`: [5](#0-4) 

The comment in the code acknowledges the design intent — "we don't want to reject the transaction notification because then it could be retried" — but this creates the opposite problem: when the burn silently fails, the ICP is permanently stranded in the CMC subaccount with no automated recovery, and the notification cannot be retried.

### Impact Explanation

This is a **ledger conservation bug**. ICP that the protocol intends to burn (destroy, reducing total supply) instead remains in the CMC's subaccount. The ICP total supply is not reduced as expected. The stranded ICP cannot be recovered by the user (the notification is finalized), and there is no automated mechanism to retry the burn. Over time, repeated transient ledger failures during high-load periods could accumulate unburned ICP in CMC subaccounts, silently inflating the effective circulating supply relative to protocol expectations.

### Likelihood Explanation

The ICP ledger can return transient errors (queue full, temporarily unavailable) under load. The CMC is a high-traffic canister. Any transient ledger error occurring in the window between cycles being minted and `burn_and_log` completing will trigger this condition. The entry path is fully reachable by any unprivileged user calling `notify_top_up`, `notify_create_canister`, or `notify_mint_cycles`. [6](#0-5) [7](#0-6) [8](#0-7) 

### Recommendation

`burn_and_log` should record failed burn attempts in durable state (e.g., a pending-burns queue) so that a background timer can retry them. Alternatively, the CMC should expose a governance-callable endpoint to retry failed burns for specific subaccounts. The current design accepts silent, unrecoverable ICP conservation failures as a trade-off against double-minting risk, but neither outcome is acceptable — the correct fix is to make the burn retryable in a way that cannot cause double-minting (e.g., by keying retries on the original notification block index).

### Proof of Concept

1. User sends ICP to CMC subaccount with `MEMO_TOP_UP_CANISTER`.
2. User calls `notify_top_up`; CMC successfully deposits cycles to the target canister via `deposit_cycles`.
3. At the moment `burn_and_log` calls `call_protobuf(ledger_canister_id, "send_pb", send_args)`, the ICP ledger returns a transient error (e.g., `SysTransient`).
4. `burn_and_log` logs `"Burning of X ICPTs from subaccount Y failed with code 2: ..."` and returns `()`.
5. `process_top_up` returns `Ok(cycles)` to `notify_top_up`.
6. `notify_top_up` records `NotificationStatus::NotifiedTopUp(Ok(cycles))` in `blocks_notified`.
7. The target canister has received its cycles. The ICP remains in the CMC subaccount. The notification is finalized and cannot be retried. The ICP is permanently unburned. [9](#0-8)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1139-1145)
```rust
#[update]
async fn notify_top_up(
    NotifyTopUp {
        block_index,
        canister_id,
    }: NotifyTopUp,
) -> Result<Cycles, NotifyError> {
```

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

**File:** rs/nns/cmc/src/main.rs (L1239-1245)
```rust
async fn notify_mint_cycles(
    NotifyMintCyclesArg {
        block_index,
        to_subaccount,
        deposit_memo,
    }: NotifyMintCyclesArg,
) -> NotifyMintCyclesResult {
```

**File:** rs/nns/cmc/src/main.rs (L1347-1355)
```rust
async fn notify_create_canister(
    NotifyCreateCanister {
        block_index,
        controller,
        subnet_type,
        subnet_selection,
        settings,
    }: NotifyCreateCanister,
) -> Result<CanisterId, NotifyError> {
```

**File:** rs/nns/cmc/src/main.rs (L1943-1946)
```rust
    match do_create_canister(controller, cycles, subnet_selection, settings).await {
        Ok(canister_id) => {
            burn_and_log(sub, amount).await;
            Ok(canister_id)
```

**File:** rs/nns/cmc/src/main.rs (L1966-1969)
```rust
    match do_mint_cycles(to_account, cycles, deposit_memo).await {
        Ok(deposit_result) => {
            burn_and_log(sub, amount).await;
            Ok(NotifyMintCyclesSuccess {
```

**File:** rs/nns/cmc/src/main.rs (L1999-2002)
```rust
    match deposit_cycles(canister_id, cycles, true, limiter_to_use).await {
        Ok(()) => {
            burn_and_log(sub, amount).await;
            Ok(cycles)
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
