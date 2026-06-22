### Title
Silent ICP Burn Failure After Cycles Minting Leads to ICP Conservation Violation — (File: `rs/nns/cmc/src/main.rs`)

### Summary

The Cycles Minting Canister (CMC) intentionally ignores the return value of the ICP burn operation that is supposed to destroy ICP after cycles are successfully minted or deposited. If the burn fails (e.g., ledger temporarily unavailable), cycles are permanently created without the corresponding ICP being destroyed, violating the ICP/cycles conservation invariant.

### Finding Description

In `rs/nns/cmc/src/main.rs`, the function `burn_and_log` is called after cycles are successfully minted (`process_mint_cycles`) or deposited (`process_top_up`). The function is explicitly designed to swallow all errors and return `()`: [1](#0-0) 

The comment at line 2015 reads: *"Burning doesn't return errors - we don't want to reject the transaction notification because then it could be retried."* The callers proceed to return success to the user regardless of whether the burn succeeded: [2](#0-1) [3](#0-2) 

The sequence is:
1. User sends ICP to CMC's subaccount.
2. User calls `notify_mint_cycles` or `notify_top_up`.
3. CMC mints/deposits cycles — **succeeds**.
4. CMC calls `burn_and_log` to destroy the ICP — **may fail silently**.
5. The notification block index is recorded as processed (preventing retry).
6. Cycles exist; ICP is not burned.

### Impact Explanation

This is a **ledger conservation bug**. The protocol invariant requires that every cycle minted corresponds to ICP being burned. When `burn_and_log` fails (e.g., the ICP ledger is temporarily unavailable during a canister upgrade), cycles are created without destroying the backing ICP. The ICP remains permanently locked in the CMC's subaccount — it cannot be recovered by the user (the notification is consumed) and is not burned. The total ICP supply is therefore higher than it should be relative to the cycles in circulation, breaking the economic conservation property of the protocol.

### Likelihood Explanation

The ICP ledger can be temporarily unavailable during routine canister upgrades or under transient network conditions. The CMC explicitly anticipates this failure mode (hence the design choice to swallow errors). Any user who calls `notify_mint_cycles` or `notify_top_up` at the moment the ledger is unavailable for the burn step will trigger this condition. The likelihood is low per individual call but non-negligible at scale over the lifetime of the protocol.

### Recommendation

The burn failure should be tracked in persistent CMC state (e.g., a queue of pending burns) so that failed burns can be retried in a subsequent timer task, similar to how the ckETH minter handles failed mints via `MINT_RETRY_DELAY`. Alternatively, the CMC should record the failed burn and expose a permissionless retry endpoint. The current design trades conservation correctness for replay-attack prevention, but a retry queue achieves both goals.

### Proof of Concept

1. User sends 1 ICP to `CMC_CANISTER_ID` subaccount keyed to their principal.
2. At the moment the user calls `notify_mint_cycles`, the ICP ledger is mid-upgrade (temporarily unavailable).
3. `do_mint_cycles` succeeds — cycles are deposited to the user's cycles-ledger account.
4. `burn_and_log` is called; `call_protobuf(ledger_canister_id, "send_pb", ...)` returns `Err(...)`.
5. The error is logged only; `burn_and_log` returns `()`.
6. `process_mint_cycles` returns `Ok(NotifyMintCyclesSuccess { ... })` to the caller.
7. The block index is recorded in `blocks_notified`; the notification cannot be replayed.
8. Result: user has cycles, 1 ICP remains unburned in the CMC subaccount, ICP total supply is inflated by 1 ICP relative to cycles minted. [4](#0-3)

### Citations

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
