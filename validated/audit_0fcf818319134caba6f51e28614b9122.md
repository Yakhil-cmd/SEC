### Title
Silently Ignored ICP Burn Result in Cycles Minting Canister Allows ICP Conservation Violation — (File: rs/nns/cmc/src/main.rs)

### Summary
The `burn_and_log` function in the Cycles Minting Canister (CMC) deliberately ignores the result of the ICP burn operation after cycles have already been minted or deposited. If the burn fails due to a transient ICP ledger error (e.g., during a subnet upgrade or temporary ledger unavailability), cycles are minted without the corresponding ICP being burned, violating the ICP/cycles conservation invariant. This is the IC analog of the ERC20 `transferFrom` result not being checked: a payment-side operation's failure is silently swallowed after the service-side state has already been committed.

### Finding Description
In `rs/nns/cmc/src/main.rs`, after cycles are successfully deposited or minted, the CMC calls `burn_and_log` to burn the ICP from its subaccount. The function explicitly swallows burn failures:

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
```

The comment is explicit: errors are intentionally not propagated to prevent the notification from being retried. The three callers all discard the return value:

- `process_top_up` — `burn_and_log(sub, amount).await;`
- `process_create_canister` — `burn_and_log(sub, amount).await;`
- `process_mint_cycles` — `burn_and_log(sub, amount).await;`

In each case, the cycles have already been deposited or minted before `burn_and_log` is called, and the `blocks_notified` entry is set to a terminal `NotifiedTopUp`/`NotifiedCreateCanister`/`NotifiedMint` status immediately after, regardless of whether the burn succeeded. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) 

### Impact Explanation
When `burn_and_log` fails silently:

1. Cycles are already irreversibly minted/deposited to the caller.
2. The ICP is **not** burned from the CMC's subaccount.
3. The `blocks_notified` entry is set to a terminal processed state — the notification cannot be retried.
4. The ICP remains orphaned in the CMC's subaccount.
5. The ICP total supply is not reduced as the protocol requires, breaking the ICP↔cycles conservation invariant.

The orphaned ICP cannot be reclaimed by the user (notification is finalized) and will not be burned automatically. Over time, repeated failures accumulate unburned ICP in CMC subaccounts, inflating the effective ICP supply relative to the cycles issued.

### Likelihood Explanation
Low-to-medium. The ICP ledger is a high-availability canister, but transient unavailability is a realistic and documented operational event: subnet upgrades, replica restarts, and transient inter-canister call rejections all produce `Err` responses from `call_protobuf`. An unprivileged user who happens to call `notify_top_up`, `notify_create_canister`, or `notify_mint_cycles` during such a window will trigger the silent failure path. No attacker capability beyond submitting a normal ingress message is required; the timing is opportunistic rather than controlled, but the window recurs with every subnet upgrade cycle.

### Recommendation
`burn_and_log` should propagate failures rather than swallow them. Two safe options:

1. **Retry via timer**: On burn failure, store the pending burn in stable state and retry it asynchronously via a heartbeat or timer, similar to how the CMC already retries other operations.
2. **Do not finalize the notification on burn failure**: Remove the `blocks_notified` terminal entry when `burn_and_log` fails, allowing the user to re-notify and trigger a fresh burn attempt. This is the direct fix analogous to using `safeTransferFrom` — the state transition is only committed once the payment leg is confirmed.

The current design comment ("we don't want to reject the transaction notification because then it could be retried") conflates two concerns: preventing double-minting (solved by the `Processing` guard) and ensuring the burn completes (currently unguarded). These can be separated cleanly.

### Proof of Concept
1. User transfers ICP to the CMC's canister-derived subaccount on the ICP ledger.
2. User calls `notify_top_up` with the block index.
3. CMC calls `fetch_transaction`, verifies the payment, sets `blocks_notified[block_index] = Processing`.
4. CMC calls `deposit_cycles` — succeeds; cycles are credited to the target canister.
5. CMC calls `burn_and_log(sub, amount).await` — the ICP ledger returns a transient error (e.g., `SysTransient` during a subnet upgrade).
6. `burn_and_log` logs the error and returns `()`.
7. CMC sets `blocks_notified[block_index] = NotifiedTopUp(Ok(cycles))` — notification is finalized.
8. Result: the target canister received cycles; the ICP was **not** burned; the notification is permanently closed; the ICP sits unburned in the CMC subaccount.

<cite repo="Camomtat/ic--016" path="rs/

### Citations

**File:** rs/nns/cmc/src/main.rs (L1209-1225)
```rust
    match maybe_early_result {
        Some(result) => result,
        None => {
            let result = process_top_up(canister_id, from, amount, limiter_to_use).await;

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
