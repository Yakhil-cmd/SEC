### Title
Unchecked ICP Burn Transfer in Cycles Minting Canister Leads to ICP Supply Inflation - (File: rs/nns/cmc/src/main.rs)

### Summary
The Cycles Minting Canister (CMC) intentionally ignores failures from the ICP burn transfer in `burn_and_log`. When the ICP ledger is temporarily unavailable at the moment of the burn call, cycles are already minted/deposited but the corresponding ICP is never burned, inflating the ICP total supply.

### Finding Description
After successfully completing a cycles-minting operation (`notify_top_up`, `notify_mint_cycles`, or `notify_create_canister`), the CMC calls `burn_and_log` to burn the user's ICP from its subaccount. This function is explicitly designed to swallow all errors: [1](#0-0) 

The function calls `send_pb` on the ICP ledger and only logs the result — it never propagates errors and never retries. The callers (`process_top_up`, `process_mint_cycles`, `process_create_canister`) all call `burn_and_log` after the cycles operation succeeds and after the block's notification status has already been committed to `NotifiedTopUp`/`NotifiedMint`/`NotifiedCreateCanister`: [2](#0-1) [3](#0-2) [4](#0-3) 

Because the block is already marked as processed before `burn_and_log` is called, the user cannot retry the notification. The ICP remains permanently stranded in the CMC's subaccount — not burned, not refundable.

### Impact Explanation
This is a **ledger conservation bug**. When `burn_and_log` fails silently:
- Cycles are already minted and in circulation (irreversible).
- The corresponding ICP is not burned — it remains in the CMC's subaccount.
- The ICP total supply is not reduced as the protocol intends, causing supply inflation.
- The stranded ICP cannot be recovered by the user (the block is marked processed) and is not automatically retried by the CMC.

The magnitude scales with the amount of ICP involved in the failed burn. Repeated occurrences (e.g., during ledger upgrade windows) compound the inflation.

### Likelihood Explanation
The ICP ledger undergoes periodic upgrades on mainnet. During an upgrade, the ledger canister is briefly stopped and rejects calls. If a `notify_top_up`/`notify_mint_cycles`/`notify_create_canister` call reaches the `burn_and_log` phase during this window, the burn silently fails. Any unprivileged user who sends ICP to the CMC and calls a notify endpoint during a ledger upgrade window can trigger this condition without any special access. No attacker coordination is required — the condition arises from normal protocol maintenance.

### Recommendation
Replace the fire-and-forget `burn_and_log` pattern with one of the following:

1. **Persist and retry**: Record failed burns in CMC state and retry them in a periodic heartbeat task, similar to how the ckBTC minter handles pending operations.
2. **Propagate the error with idempotency**: Return an error to the caller but use the existing `blocks_notified` deduplication map to prevent double-spending on retry — only clear the `Processing` status on transient ledger errors, not on permanent ones.
3. **Pre-burn before minting**: Burn the ICP before minting cycles, so that a burn failure prevents cycles from being issued rather than leaving ICP unburned after the fact.

### Proof of Concept
1. User sends 10 ICP to `CMC_SUBACCOUNT(canister_id)` with memo `MEMO_TOP_UP_CANISTER`.
2. User calls `notify_top_up { block_index, canister_id }`.
3. CMC calls `fetch_transaction` (succeeds), sets block status to `Processing`, calls `process_top_up`.
4. `process_top_up` calls `deposit_cycles` — succeeds, cycles are deposited to `canister_id`.
5. `process_top_up` calls `burn_and_log` — at this moment the ICP ledger is being upgraded and rejects the `send_pb` call.
6. `burn_and_log` logs the error and returns `()`.
7. CMC sets block status to `NotifiedTopUp(Ok(cycles))` and returns `Ok(cycles)` to the user.
8. The 10 ICP remain in `CMC_SUBACCOUNT(canister_id)`, unburned. The ICP total supply is 10 ICP higher than it should be. The user cannot retry (block already processed). The ICP is permanently stranded. [5](#0-4)

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
