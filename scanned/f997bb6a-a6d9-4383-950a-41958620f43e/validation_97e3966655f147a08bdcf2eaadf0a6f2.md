### Title
`burn_and_log` Silently Discards ICP Ledger Burn Failure, Breaking ICP Supply Conservation - (File: `rs/nns/cmc/src/main.rs`)

### Summary

The Cycles Minting Canister (CMC) function `burn_and_log` performs an ICP ledger `send_pb` call to burn ICP after successfully minting cycles or creating a canister. The call's failure result is only logged — never propagated or retried — so if the ledger is transiently unavailable (e.g., during an upgrade), the ICP is silently left unburned in the CMC subaccount while the corresponding cycles have already been minted and delivered. This breaks the ICP supply conservation invariant.

### Finding Description

`burn_and_log` is called as the final step in three CMC notification flows after the primary operation has already succeeded:

- `process_create_canister` → canister created → `burn_and_log(sub, amount).await`
- `process_top_up` → cycles deposited → `burn_and_log(sub, amount).await`
- `process_mint_cycles` → cycles minted to cycles ledger → `burn_and_log(sub, amount).await` [1](#0-0) 

The function issues a `send_pb` call to the ICP ledger: [2](#0-1) 

On error, it only prints a log line. No error is returned, no retry is scheduled, and no compensating action is taken. The block is already recorded as `NotifiedTopUp` / `NotifiedCreateCanister` / `NotifiedMint` before `burn_and_log` is awaited, so the user cannot re-notify to trigger the burn again. [3](#0-2) [4](#0-3) [5](#0-4) 

The code comment explicitly acknowledges this design: *"Burning doesn't return errors — we don't want to reject the transaction notification because then it could be retried."* The intent is to prevent double-minting, but the consequence is that a failed burn leaves ICP permanently unburned and stranded in the CMC subaccount.

### Impact Explanation

**Ledger conservation bug.** When `burn_and_log` fails:

1. The ICP that was sent to the CMC subaccount is **not burned** — it remains in the CMC subaccount indefinitely.
2. The corresponding cycles have already been minted and delivered to the beneficiary.
3. The block is marked as fully processed; neither the user nor the CMC can retry the burn.
4. The total ICP supply is higher than it should be (ICP that should have been destroyed is not), violating the ICP conservation invariant that underpins the ICP↔cycles exchange rate.

Over repeated occurrences (e.g., across multiple ledger upgrades), unburned ICP accumulates in CMC subaccounts, silently inflating the circulating ICP supply relative to the cycles minted.

### Likelihood Explanation

The ICP ledger is upgraded periodically via NNS governance proposals. During an upgrade, the ledger canister is briefly stopped and unavailable. Any `notify_top_up`, `notify_create_canister`, or `notify_mint_cycles` call that completes its primary operation (cycles deposited / canister created) just before or during the ledger upgrade window will trigger this path. No special attacker capability is required — any unprivileged user calling these endpoints during a ledger upgrade window can cause the burn to fail silently. The ledger upgrade window is observable on-chain, making timing feasible.

### Recommendation

`burn_and_log` should be redesigned to handle burn failures durably:

1. **Persist the pending burn** in CMC stable state before marking the block as fully processed.
2. **Retry the burn** in a heartbeat or timer task until it succeeds.
3. Alternatively, record the unburned amount and expose a governance-callable recovery endpoint.

The current trade-off (no retry to prevent double-minting) is valid for the primary operation, but the burn step is idempotent and safe to retry — a duplicate burn attempt on an already-burned subaccount will simply fail with insufficient funds, not produce a double burn.

### Proof of Concept

1. User sends ICP to CMC subaccount with `MEMO_TOP_UP_CANISTER`.
2. User calls `notify_top_up` on CMC.
3. CMC calls `deposit_cycles` → succeeds; cycles are delivered to the target canister.
4. CMC calls `burn_and_log` → the ICP ledger is mid-upgrade and returns a reject.
5. `burn_and_log` logs the error and returns `()`.
6. CMC records `NotificationStatus::NotifiedTopUp(Ok(cycles))` for the block.
7. The ICP remains in the CMC subaccount. The block cannot be re-notified. The ICP is permanently unburned.

Relevant call chain: [6](#0-5) [7](#0-6)

### Citations

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
