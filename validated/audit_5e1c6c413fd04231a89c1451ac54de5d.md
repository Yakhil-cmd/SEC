### Title
Order-Dependent State Update in CMC: Cycles Minted Before ICP Burned, Creating Transient ICP Supply Inflation - (File: rs/nns/cmc/src/main.rs)

### Summary

The Cycles Minting Canister (CMC) in `rs/nns/cmc/src/main.rs` contains an order-dependent state update vulnerability analogous to the reported `VaultFundManager` bug. In `process_mint_cycles`, `process_top_up`, and `process_create_canister`, cycles are fully minted/deposited to the recipient **before** the corresponding ICP is burned from the CMC's subaccount. The `burn_and_log` call is explicitly fire-and-forget: it swallows errors silently and does not block success. This creates a window — and in the failure case a permanent state — where cycles exist in circulation without the backing ICP having been destroyed, violating the ICP↔cycles conservation invariant.

---

### Finding Description

In `process_mint_cycles` (line 1966–1968), `process_top_up` (line 1999–2001), and `process_create_canister` (line 1943–1945), the operation sequence is:

1. **Step 1**: Mint/deposit cycles to the recipient (cross-canister call to cycles ledger or management canister).
2. **Step 2**: Call `burn_and_log(sub, amount)` to burn the ICP from the CMC's subaccount.

`burn_and_log` is explicitly designed to never return an error:

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

If `burn_and_log` fails (ledger temporarily unavailable, trap, or any transient error), the cycles have already been credited to the recipient and the notification is marked `NotifiedMint`/`NotifiedTopUp` (non-retryable), but the ICP sitting in the CMC's subaccount is **never burned**. The ICP remains in the CMC's subaccount indefinitely with no retry mechanism.

The intermediate state during normal execution is also observable: between the `do_mint_cycles` return and the `burn_and_log` completion, the ICP total supply is inflated (ICP not yet burned) while cycles are already in circulation.

The `ensure_balance` / limiter accounting in `do_mint_cycles` (line 2151) updates `total_cycles_minted` and the rate limiter **before** the ICP burn, so the limiter correctly counts the cycles but the ICP conservation is broken if the burn fails. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

---

### Impact Explanation

**Ledger conservation bug**: If `burn_and_log` fails after cycles are minted, the ICP total supply is permanently inflated by the amount that was not burned. The ICP sits stranded in the CMC's subaccount (the `Subaccount::from(&caller())` or `Subaccount::from(&canister_id)`) with no automated recovery path. The notification is marked as completed (`NotifiedMint`), so the user cannot retry, and the ICP is not refunded. This breaks the ICP↔cycles conservation invariant: cycles exist in circulation without corresponding ICP destruction.

During normal execution, the transient window where cycles are credited but ICP is not yet burned means that any concurrent `icrc1_total_supply` query on the ICP ledger will observe an inflated supply, and any `total_cycles_minted` query on the CMC will show cycles that have not yet been backed by a burn — a temporary but observable inconsistency. [5](#0-4) [6](#0-5) 

---

### Likelihood Explanation

The `burn_and_log` failure path is reachable by any unprivileged user who calls `notify_mint_cycles`, `notify_top_up`, or `notify_create_canister`. The ICP ledger is a separate canister; transient unavailability (e.g., during ledger upgrades, subnet congestion, or message queue overflow) can cause the `send_pb` call inside `burn_and_log` to fail. The CMC explicitly acknowledges this risk in the comment: *"Burning doesn't return errors - we don't want to reject the transaction notification because then it could be retried."* This design choice accepts the burn failure silently. Any user who times their `notify_mint_cycles` call to coincide with a ledger upgrade window can trigger this condition. [7](#0-6) [8](#0-7) 

---

### Recommendation

Reorder the operations so that the ICP burn is confirmed **before** cycles are minted/deposited. The corrected sequence should be:

1. Burn the ICP from the CMC subaccount (call ledger `send_pb` to minting account).
2. Only if the burn succeeds, mint/deposit cycles to the recipient.

If the burn fails, return an error that allows the user to retry. This mirrors the fix recommended in the external report: complete the destructive/debit operation before the credit operation. Alternatively, implement a persistent retry queue for failed burns so that stranded ICP is eventually recovered. [9](#0-8) [10](#0-9) 

---

### Proof of Concept

**Attacker-controlled entry path**: Any unprivileged user with ICP.

1. User transfers ICP to the CMC subaccount with `MEMO_MINT_CYCLES`.
2. User calls `notify_mint_cycles` on the CMC.
3. CMC calls `do_mint_cycles` → cycles ledger `deposit` → cycles credited to user. ✓
4. CMC calls `burn_and_log` → ICP ledger `send_pb` → **ledger is temporarily unavailable** (e.g., during upgrade) → call fails.
5. `burn_and_log` logs the error and returns `()` silently.
6. `notify_mint_cycles` returns `Ok(NotifyMintCyclesSuccess {...})` to the user.
7. The block is marked `NotifiedMint` — non-retryable.
8. **Result**: User has received cycles. ICP in CMC subaccount is never burned. ICP total supply is permanently inflated by `amount`. No automated recovery exists.

The same path applies to `notify_top_up` (via `process_top_up` → `deposit_cycles` → `burn_and_log`) and `notify_create_canister` (via `process_create_canister` → `do_create_canister` → `burn_and_log`). [11](#0-10) [12](#0-11) [13](#0-12)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1209-1226)
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
    }
```

**File:** rs/nns/cmc/src/main.rs (L1299-1316)
```rust
    match maybe_early_result {
        Some(result) => result,
        None => {
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

**File:** rs/nns/cmc/src/main.rs (L1925-1956)
```rust
async fn process_create_canister(
    controller: PrincipalId,
    from: AccountIdentifier,
    amount: Tokens,
    subnet_selection: Option<SubnetSelection>,
    settings: Option<CanisterSettings>,
) -> Result<CanisterId, NotifyError> {
    let cycles = tokens_to_cycles(amount)?;

    let sub = Subaccount::from(&controller);

    print(format!(
        "Creating canister with controller {controller} with {cycles} cycles.",
    ));

    // Create the canister. If this fails, refund. Either way,
    // return a result so that the notification cannot be retried.
    // If refund fails, we allow to retry.
    match do_create_canister(controller, cycles, subnet_selection, settings).await {
        Ok(canister_id) => {
            burn_and_log(sub, amount).await;
            Ok(canister_id)
        }
        Err(err) => {
            let refund_block = refund_icp(sub, from, amount, CREATE_CANISTER_REFUND_FEE).await?;
            Err(NotifyError::Refunded {
                reason: err,
                block_index: refund_block,
            })
        }
    }
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

**File:** rs/nns/cmc/src/main.rs (L2140-2172)
```rust
async fn do_mint_cycles(
    account: Account,
    cycles: Cycles,
    deposit_memo: Option<Vec<u8>>,
) -> Result<CyclesLedgerDepositResult, String> {
    let Some(cycles_ledger_canister_id) = with_state(|state| state.cycles_ledger_canister_id)
    else {
        return Err("No cycles ledger canister id configured.".to_string());
    };
    // Always use base cycles limit for minting cycles, since the Subnet Rental Canister
    // doesn't call endpoints using this function.
    ensure_balance(cycles, CyclesMintingLimiterSelector::BaseLimit)?;

    let arg = CyclesLedgerDepositArgs {
        to: account,
        memo: deposit_memo,
    };

    let result: CallResult<(CyclesLedgerDepositResult,)> = ic_cdk::api::call::call_with_payment128(
        cycles_ledger_canister_id.get().0,
        "deposit",
        (arg,),
        u128::from(cycles),
    )
    .await;

    result.map(|r| r.0).map_err(|(code, msg)| {
        format!(
            "Cycles ledger rejected deposit call with code {}: {:?}",
            code as i32, msg
        )
    })
}
```

**File:** rs/nns/cmc/src/main.rs (L2306-2325)
```rust
fn ensure_balance(
    cycles: Cycles,
    limiter_to_use: CyclesMintingLimiterSelector,
) -> Result<(), String> {
    let now = now_system_time();

    let current_balance = Cycles::from(ic_cdk::api::canister_balance128());
    let cycles_to_mint = cycles - current_balance;

    with_state_mut(|state| {
        limiter_to_use.check_and_add_cycles(state, now, cycles_to_mint)?;
        state.total_cycles_minted += cycles_to_mint;
        Ok::<_, String>(())
    })?;

    // unused because of check above
    let _minted_cycles = ic0_mint_cycles128(cycles_to_mint);
    assert!(ic_cdk::api::canister_balance128() >= cycles.get());
    Ok(())
}
```
