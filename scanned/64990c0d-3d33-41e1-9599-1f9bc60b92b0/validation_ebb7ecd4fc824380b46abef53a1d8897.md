### Title
Unchecked ICP Burn Result Allows Silent ICP Supply Conservation Failure - (File: rs/nns/cmc/src/main.rs)

### Summary
The Cycles Minting Canister (CMC) intentionally swallows the result of the ICP burn operation in `burn_and_log`. After successfully delivering cycles or creating a canister, the CMC calls `burn_and_log` to destroy the user's ICP. If this ledger call fails, the ICP is never burned, cycles/canisters have already been delivered, and the ICP remains stranded in the CMC's subaccount — permanently inflating the ICP supply relative to the cycles issued.

### Finding Description
In `rs/nns/cmc/src/main.rs`, the function `burn_and_log` is called after every successful `process_top_up`, `process_create_canister`, and `process_mint_cycles` operation. The function explicitly discards the ledger call result:

```rust
// rs/nns/cmc/src/main.rs:2014-2048
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

The callers proceed unconditionally after `burn_and_log` returns, regardless of whether the burn succeeded:

```rust
// process_top_up (line 1999-2002)
match deposit_cycles(canister_id, cycles, true, limiter_to_use).await {
    Ok(()) => {
        burn_and_log(sub, amount).await;  // result silently discarded
        Ok(cycles)
    }
```

```rust
// process_create_canister (line 1943-1946)
match do_create_canister(controller, cycles, subnet_selection, settings).await {
    Ok(canister_id) => {
        burn_and_log(sub, amount).await;  // result silently discarded
        Ok(canister_id)
    }
```

```rust
// process_mint_cycles (line 1966-1973)
match do_mint_cycles(to_account, cycles, deposit_memo).await {
    Ok(deposit_result) => {
        burn_and_log(sub, amount).await;  // result silently discarded
        Ok(NotifyMintCyclesSuccess { ... })
    }
```

This is the direct IC analog of the ERC20 unchecked `transfer` return value pattern: a token movement operation's failure is silently swallowed, and the caller proceeds as if it succeeded.

### Impact Explanation
When `burn_and_log` fails (e.g., due to a transient ICP ledger rejection or unavailability):

1. Cycles have already been deposited to the target canister, or a canister has been created, or cycles-ledger tokens have been minted.
2. The ICP that should have been destroyed remains in the CMC's per-operation subaccount.
3. The notification is marked as `NotifiedTopUp`/`NotifiedCreateCanister`/`NotifiedMint` — it cannot be retried.
4. The ICP is permanently stranded in the CMC's subaccount with no recovery path.
5. The ICP supply is not reduced as the protocol requires: cycles were issued against ICP that was never burned, breaking the ICP↔cycles conservation invariant.

This is a **ledger conservation bug**: the total ICP supply is higher than it should be relative to the cycles in circulation, violating the economic invariant that underpins the ICP/cycles exchange.

### Likelihood Explanation
The ICP ledger can transiently reject calls during upgrades, subnet maintenance, or under high load. The CMC is a high-traffic canister processing many `notify_top_up` calls. Over time, even a low per-call failure rate accumulates into a measurable supply discrepancy. The entry path is fully unprivileged: any user who sends ICP to the CMC and calls `notify_top_up`, `notify_create_canister`, or `notify_mint_cycles` can trigger this code path. The user does not need to do anything special — the burn failure is a consequence of ledger unavailability at the moment the CMC attempts it.

### Recommendation
`burn_and_log` should propagate its error result back to the callers. On burn failure, the CMC should either:
1. Retry the burn (with a bounded retry count), or
2. Record the failed burn in persistent state so that a background timer or subsequent call can retry it, ensuring the ICP is eventually destroyed.

The current design comment ("we don't want to reject the transaction notification because then it could be retried") is valid for the *notification idempotency* concern, but the burn retry can be decoupled from the notification status — the notification can remain marked as processed while the burn is retried independently.

### Proof of Concept
1. User sends N ICP to `CMC_subaccount_for_canister_X`.
2. User calls `notify_top_up { block_index, canister_id: X }`.
3. CMC fetches the transaction, marks block as `Processing`, calls `deposit_cycles` — succeeds, cycles delivered to canister X.
4. CMC calls `burn_and_log(sub, amount)`. At this moment the ICP ledger is being upgraded and rejects the call with a transient error.
5. `burn_and_log` logs the error and returns `()`.
6. CMC marks block as `NotifiedTopUp(Ok(cycles))` and returns `Ok(cycles)` to the user.
7. Result: canister X received cycles, user's ICP notification is permanently consumed, but the N ICP remain in the CMC's subaccount unburned. The ICP supply is inflated by N ICP relative to the cycles issued. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

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

**File:** rs/nns/cmc/src/main.rs (L1999-2002)
```rust
    match deposit_cycles(canister_id, cycles, true, limiter_to_use).await {
        Ok(()) => {
            burn_and_log(sub, amount).await;
            Ok(cycles)
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
