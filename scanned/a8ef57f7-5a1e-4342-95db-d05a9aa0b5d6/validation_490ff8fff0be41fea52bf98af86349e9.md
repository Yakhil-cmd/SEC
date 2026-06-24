### Title
Unchecked ICP Burn Result in CMC `burn_and_log` Allows Cycles Minting Without ICP Conservation - (File: rs/nns/cmc/src/main.rs)

---

### Summary

The Cycles Minting Canister (CMC) mints cycles before burning the corresponding ICP. The burn is performed via `burn_and_log`, which explicitly swallows all errors and returns `()`. If the ICP ledger rejects or fails the burn call, cycles are permanently minted without the backing ICP being destroyed, breaking the ICP-to-cycles conservation invariant.

---

### Finding Description

In `rs/nns/cmc/src/main.rs`, the function `burn_and_log` is responsible for burning ICP after cycles have been successfully minted or deposited: [1](#0-0) 

The function signature returns `()` and the match arm on error only calls `print(...)` — the error is never propagated. The callers `process_mint_cycles` and `process_top_up` invoke it as a fire-and-forget: [2](#0-1) 

The ordering is critical: `do_mint_cycles` (or `deposit_cycles`) succeeds first, then `burn_and_log` is called. If the burn fails, the cycles are already minted and the notification is recorded as `NotifiedMint` in `blocks_notified`, preventing any retry: [3](#0-2) 

The same pattern applies to `process_top_up`: [4](#0-3) 

The design comment explicitly acknowledges this: *"Burning doesn't return errors - we don't want to reject the transaction notification because then it could be retried."* This prevents double-minting but creates a silent conservation failure path.

---

### Impact Explanation

**Vulnerability class: Ledger conservation bug.**

When `burn_and_log` fails:
1. Cycles are minted (or a canister is topped up) — the user receives full value.
2. The ICP in CMC's subaccount is **not burned** — it remains locked in CMC's subaccount permanently (the block index is marked as processed, so no retry is possible).
3. The total ICP supply is higher than it should be relative to the cycles minted, breaking the economic invariant that ICP burned = cycles minted.
4. There is no recovery path: the ICP is stuck in CMC's subaccount with no mechanism to retry the burn.

This is a direct analog to the ERC20 SafeERC20 issue: a token operation (`send_pb` burn transfer) returns an error that the calling contract (CMC) does not check, allowing the state to diverge from the expected conservation invariant.

---

### Likelihood Explanation

The ICP ledger (`send_pb` via `call_protobuf`) can fail in the following realistic scenarios:
- **Ledger canister upgrade**: During a canister stop/upgrade cycle, calls to the ledger are rejected with a transient error. A user who times `notify_mint_cycles` to complete its cycles-deposit step just before a ledger upgrade window would trigger this path.
- **Transient inter-canister call rejection**: The IC can reject inter-canister calls under resource pressure or if the ledger's message queue is full.
- **Ledger trap/panic**: Any bug or trap in the ledger's `send_pb` handler returns an error to the caller.

The user cannot reliably force this condition on demand, but ledger upgrades are publicly announced and occur regularly on mainnet. The likelihood is **low but non-zero**, and the impact per occurrence is permanent ICP supply inflation.

---

### Recommendation

1. **Record failed burns**: If `burn_and_log` fails, store the `(subaccount, amount)` pair in persistent CMC state and retry the burn in a background timer, similar to how ckBTC/ckETH handle failed mints.
2. **Alternatively, reverse the order**: Burn the ICP first, then mint cycles. If the burn fails, return an error to the caller. This is the pattern used by ckBTC (`burn_ckbtcs` → then submit BTC withdrawal) and ckETH (`burn_from` → then process withdrawal).
3. **At minimum, emit a metric**: Expose a counter for failed burns so operators can detect and manually remediate stuck ICP.

---

### Proof of Concept

**Entry path** (unprivileged ingress sender):

1. User sends ICP to CMC's subaccount with `MEMO_MINT_CYCLES`:
   ```
   icp_ledger.transfer({ to: cmc_subaccount(caller), memo: MEMO_MINT_CYCLES, amount: X })
   ```
2. User calls `notify_mint_cycles` on CMC. CMC calls `do_mint_cycles` → cycles ledger `deposit` succeeds → cycles are credited to user.
3. CMC calls `burn_and_log(sub, amount)`. At this moment, if the ICP ledger is temporarily unavailable (e.g., mid-upgrade), `call_protobuf(..., "send_pb", ...)` returns `Err(...)`.
4. `burn_and_log` logs the error and returns `()`. CMC records `NotificationStatus::NotifiedMint(Ok(...))` for the block index.
5. **Result**: User has received cycles. ICP in CMC's subaccount is not burned. The block index is permanently marked as processed — no retry is possible. ICP supply is inflated by `X` tokens relative to cycles minted. [5](#0-4) [6](#0-5)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1302-1316)
```rust
            let result =
                process_mint_cycles(to_account, amount, deposit_memo, from, subaccount).await;

            with_state_mut(|state| {
                state.blocks_notified.insert(
                    block_index,
                    NotificationStatus::NotifiedMint(result.clone()),
                );
                if is_transient_error(&result) {
                    state.blocks_notified.remove(&block_index);
                }
            });

            result
        }
```

**File:** rs/nns/cmc/src/main.rs (L1958-1983)
```rust
async fn process_mint_cycles(
    to_account: Account,
    amount: Tokens,
    deposit_memo: Option<Vec<u8>>,
    from: AccountIdentifier,
    sub: Subaccount,
) -> NotifyMintCyclesResult {
    let cycles = tokens_to_cycles(amount)?;
    match do_mint_cycles(to_account, cycles, deposit_memo).await {
        Ok(deposit_result) => {
            burn_and_log(sub, amount).await;
            Ok(NotifyMintCyclesSuccess {
                block_index: deposit_result.block_index,
                minted: cycles.into(),
                balance: deposit_result.balance,
            })
        }
        Err(err) => {
            let refund_block = refund_icp(sub, from, amount, MINT_CYCLES_REFUND_FEE).await?;
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
