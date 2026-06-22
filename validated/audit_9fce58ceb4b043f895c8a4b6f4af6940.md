### Title
Silent ICP Burn Failure in Cycles Minting Canister Breaks ICP/Cycles Conservation Invariant - (File: rs/nns/cmc/src/main.rs)

### Summary
The `burn_and_log` function in the Cycles Minting Canister (CMC) intentionally discards the return value of the ICP ledger burn transfer. After cycles are successfully deposited, if the ICP burn call fails, the failure is only logged — the notification is still committed as a non-retriable success. This is the direct IC analog of the original finding: a transfer whose failure result is swallowed, allowing the system to proceed as if the transfer succeeded.

### Finding Description
After successfully depositing cycles (via `deposit_cycles` or `do_mint_cycles`), the CMC calls `burn_and_log` to destroy the corresponding ICP. The function signature returns `()` unconditionally:

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
```

The design comment explicitly states: *"Burning doesn't return errors — we don't want to reject the transaction notification because then it could be retried."* [1](#0-0) 

This function is called in three flows after the primary action already succeeded:

- `process_top_up` — after `deposit_cycles` succeeds
- `process_create_canister` — after `do_create_canister` succeeds
- `process_mint_cycles` — after `do_mint_cycles` succeeds [2](#0-1) [3](#0-2) [4](#0-3) 

In all three cases, the notification is then committed to `blocks_notified` as a permanent, non-retriable success: [5](#0-4) 

### Impact Explanation
If the ICP ledger's `send_pb` call fails during `burn_and_log` (e.g., due to a transient reject, ledger upgrade, or canister queue full), the following state results:

1. Cycles have been minted and deposited to the target canister (irreversible).
2. The ICP has **not** been burned — it remains in the CMC's subaccount (keyed by `canister_id` or `controller`).
3. The block index is permanently recorded as `NotifiedTopUp(Ok(...))`, preventing any retry.
4. The unburned ICP is effectively stranded in the CMC subaccount with no recovery path.

This breaks the ICP/cycles conservation invariant: the ICP total supply is higher than it should be while the corresponding cycles have already been created. The magnitude equals the full `amount` of ICP that failed to burn.

### Likelihood Explanation
The ICP ledger can be temporarily unavailable during subnet upgrades, canister upgrades, or under high message queue pressure. The window between `deposit_cycles` completing and `burn_and_log` executing is a real inter-canister async gap. Any transient reject from the ledger during this window silently breaks the invariant. This is not a theoretical path — ledger upgrades occur regularly on the NNS subnet.

### Recommendation
The CMC should not silently discard burn failures. Options include:

1. **Retry queue**: Record failed burns in stable state and retry them on a timer, similar to how SNS governance retries `disburse_maturity_in_progress` entries.
2. **Pre-commit burn**: Attempt the burn before marking the notification as processed, and only mark it processed after both the cycle deposit and the burn succeed. The retry-attack concern can be addressed with idempotency keys rather than by swallowing errors.
3. **At minimum**: Expose a metric or certified variable tracking unburned ICP so operators can detect and manually recover from conservation violations.

### Proof of Concept

1. User sends 10 ICP to the CMC subaccount for `canister_id = X`.
2. User calls `notify_top_up { block_index: B, canister_id: X }`.
3. CMC calls `fetch_transaction` — verifies the 10 ICP payment at block `B`. ✓
4. CMC calls `deposit_cycles(X, cycles, ...)` — cycles successfully deposited to canister X. ✓
5. ICP ledger is momentarily unavailable (e.g., mid-upgrade). The `send_pb` call in `burn_and_log` returns `Err(...)`.
6. `burn_and_log` logs the error and returns `()`. The 10 ICP remain in the CMC subaccount.
7. CMC inserts `NotifiedTopUp(Ok(cycles))` into `blocks_notified` — permanently non-retriable.
8. **Result**: Canister X has the cycles. The 10 ICP are unburned and stranded. ICP total supply is 10 ICP higher than the protocol invariant requires. [6](#0-5)

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
