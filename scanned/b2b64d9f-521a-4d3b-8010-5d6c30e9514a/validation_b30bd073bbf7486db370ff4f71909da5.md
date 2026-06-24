### Title
Silent ICP Burn Failure After Successful Cycles Minting Inflates ICP Total Supply - (File: `rs/nns/cmc/src/main.rs`)

### Summary
The Cycles Minting Canister (CMC) intentionally swallows ICP ledger burn errors after cycles have already been minted or deposited. If the ICP ledger call inside `burn_and_log` fails for any reason, the ICP tokens remain permanently unburned in the CMC's subaccount while the notification is already marked as processed and cannot be retried. This is an IC-native ledger conservation bug analogous to the ERC20 unchecked-transfer-return-value class: a critical token destruction operation's failure is silently discarded.

### Finding Description

After a user successfully receives cycles (via `notify_top_up`, `notify_create_canister`, or `notify_mint_cycles`), the CMC calls `burn_and_log` to destroy the corresponding ICP from its subaccount. The function is explicitly designed to never propagate errors:

```rust
/// Attempt to burn the funds.
/// Burning doesn't return errors - we don't want to reject the transaction
/// notification because then it could be retried.
async fn burn_and_log(from_subaccount: Subaccount, amount: Tokens) {
    // ...
    let res: CallResult<BlockIndex> = call_protobuf(ledger_canister_id, "send_pb", send_args).await;
    match res {
        Ok(block) => print(format!("{msg} done in block {block}.")),
        Err((code, err)) => {
            let code = code as i32;
            print(format!("{msg} failed with code {code}: {err:?}"))  // silently logged only
        }
    }
}
``` [1](#0-0) 

This function is called on the success path of all three notification flows:

- `process_create_canister`: after `do_create_canister` succeeds → `burn_and_log(sub, amount).await`
- `process_top_up`: after `deposit_cycles` succeeds → `burn_and_log(sub, amount).await`
- `process_mint_cycles`: after `do_mint_cycles` succeeds → `burn_and_log(sub, amount).await` [2](#0-1) [3](#0-2) [4](#0-3) 

By the time `burn_and_log` is called, the notification has already been recorded as a terminal success state (`NotifiedTopUp(Ok(...))`, `NotifiedCreateCanister(Ok(...))`, or `NotifiedMint(Ok(...))`): [5](#0-4) 

The `is_transient_error` removal guard only applies to the outer `process_*` result, not to the inner `burn_and_log` failure. Once the notification is committed as a success, it cannot be retried.

### Impact Explanation

If `burn_and_log` fails (e.g., the ICP ledger is temporarily unavailable at the exact moment the burn is attempted, which is a documented IC failure mode), the following state results:

1. The user has received their cycles / canister / cycle-ledger balance — the primary operation succeeded.
2. The ICP tokens remain in the CMC's subaccount permanently. The user cannot retrieve them (the notification is consumed), but they are also not destroyed.
3. The ICP total supply is inflated relative to the amount of cycles minted. The invariant `ICP burned = cycles minted / conversion_rate` is violated.

This is a **ledger conservation bug**: ICP that should be destroyed persists, inflating the circulating supply. The magnitude scales with the amount of ICP involved in the failed burn.

### Likelihood Explanation

The ICP ledger is a highly available system canister, but transient inter-canister call failures are a documented and handled failure mode throughout the IC codebase (evidenced by `TemporarilyUnavailable` error variants everywhere). The CMC itself handles transient errors in the outer `process_*` functions via `is_transient_error`. The window of vulnerability is narrow — the ledger must be unavailable specifically during the `burn_and_log` call after the primary operation succeeds — but this is a realistic scenario during ledger upgrades, subnet stress, or message queue saturation. Any user who sends ICP to the CMC and triggers a notification during such a window would cause unburned ICP to persist.

### Recommendation

`burn_and_log` should be made retryable. One approach: if the burn fails, record the pending burn in CMC state and retry it on a timer, similar to how ckBTC minter handles failed reimbursements via `reimburse_withdrawals`. The notification can still be marked as a success (to prevent double-spending of cycles), but the burn should be retried until it succeeds. Alternatively, the CMC could record a "pending burn" queue and process it separately, ensuring the ICP supply invariant is eventually restored. [6](#0-5) 

### Proof of Concept

1. User sends N ICP to `CMC_subaccount(user_principal)` with `MEMO_TOP_UP_CANISTER`.
2. User calls `notify_top_up { block_index, canister_id }`.
3. CMC calls `deposit_cycles(canister_id, cycles, ...)` — succeeds.
4. CMC records `blocks_notified[block_index] = NotifiedTopUp(Ok(cycles))`.
5. CMC calls `burn_and_log(sub, amount)` — the ICP ledger returns a transient error (e.g., `CanisterError` during ledger upgrade).
6. `burn_and_log` logs the error and returns `()` — no state change, no retry scheduled.
7. Result: user's canister has the cycles; N ICP remain in `CMC_subaccount(user_principal)` unburned; `notify_top_up` with the same `block_index` returns the cached `Ok(cycles)` — the burn can never be retried through the normal flow.
8. ICP total supply is inflated by N tokens. [7](#0-6) [8](#0-7)

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

**File:** rs/bitcoin/ckbtc/minter/src/reimbursement/mod.rs (L58-116)
```rust
pub async fn reimburse_withdrawals<R: CanisterRuntime>(runtime: &R) {
    if state::read_state(|s| s.pending_withdrawal_reimbursements.is_empty()) {
        return;
    }
    let pending_reimbursements = state::read_state(|s| s.pending_withdrawal_reimbursements.clone());
    let mut error_count = 0;
    for (burn_index, reimbursement) in pending_reimbursements {
        // Ensure that even if we were to panic in the callback, after having contacted the ledger to mint the tokens,
        // this reimbursement request will not be processed again.
        let prevent_double_minting_guard = scopeguard::guard(burn_index, |index| {
            state::mutate_state(|s| {
                state::audit::quarantine_withdrawal_reimbursement(s, index, runtime)
            });
        });
        let memo = MintMemo::ReimburseWithdrawal {
            withdrawal_id: burn_index,
        };
        match runtime
            .mint_ckbtc(
                reimbursement.amount,
                reimbursement.account,
                Memo::from(crate::memo::encode(&memo)),
            )
            .await
        {
            Ok(mint_index) => {
                log!(
                    Priority::Debug,
                    "[reimburse_withdrawals]: Successfully reimbursed {:?} at mint block index {}",
                    reimbursement,
                    mint_index
                );
                state::mutate_state(|s| {
                    state::audit::reimburse_withdrawal_completed(s, burn_index, mint_index, runtime)
                });
            }
            Err(err) => {
                log!(
                    Priority::Info,
                    "[reimburse_withdrawals]: Failed to reimburse {:?}: {:?}. Will retry later",
                    reimbursement,
                    err
                );
                error_count += 1;
            }
        }
        // Defuse the guard. Note that in case of a panic in the callback (either before or after this point)
        // the defuse will not be effective (due to state rollback), and the guard that was
        // setup before the `mint_ckbtc` async call will be invoked.
        scopeguard::ScopeGuard::into_inner(prevent_double_minting_guard);
    }

    if error_count > 0 {
        log!(
            Priority::Info,
            "[reimburse_withdrawals] Failed to reimburse {error_count} withdrawal requests, retrying later."
        );
    }
}
```
