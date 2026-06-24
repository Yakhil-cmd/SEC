### Title
Cycles Minted Without Guaranteed ICP Burn in Cycles Minting Canister — (`File: rs/nns/cmc/src/main.rs`)

### Summary

The Cycles Minting Canister (CMC) mints and deposits cycles to a caller before burning the backing ICP. The ICP burn step (`burn_and_log`) is intentionally designed to silently swallow all errors. If the ICP ledger call fails after cycles have already been deposited, the ICP is never burned, inflating the cycles supply without a corresponding ICP destruction — breaking the ICP↔cycles conservation invariant.

### Finding Description

The CMC's `notify_top_up`, `notify_mint_cycles`, and `notify_create_canister` flows all follow the same pattern:

1. Mint cycles via `ensure_balance` → `ic0_mint_cycles128`
2. Deposit cycles to the target canister or cycles ledger
3. Call `burn_and_log` to destroy the backing ICP

The critical issue is in step 3. `burn_and_log` is explicitly designed to never propagate errors:

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
            print(format!("{msg} failed with code {code}: {err:?}"))  // silently logged, not propagated
        }
    }
}
``` [1](#0-0) 

This is called after cycles are already irreversibly deposited:

```rust
// process_top_up: cycles deposited first, then burn attempted
match deposit_cycles(canister_id, cycles, true, limiter_to_use).await {
    Ok(()) => {
        burn_and_log(sub, amount).await;  // burn can silently fail here
        Ok(cycles)
    }
    ...
}
``` [2](#0-1) 

```rust
// process_mint_cycles: same pattern
match do_mint_cycles(to_account, cycles, deposit_memo).await {
    Ok(deposit_result) => {
        burn_and_log(sub, amount).await;  // burn can silently fail here
        Ok(NotifyMintCyclesSuccess { ... })
    }
    ...
}
``` [3](#0-2) 

Additionally, `burn_and_log` has an early-return path that silently skips the burn entirely when `amount < DEFAULT_TRANSFER_FEE`:

```rust
if amount < DEFAULT_TRANSFER_FEE {
    print(format!("{msg}: amount too small ({amount})"));
    return;
}
``` [4](#0-3) 

This path is reachable in `refund_icp` where `burn_and_log` is called with `burned = extra_fee`, which can be smaller than `DEFAULT_TRANSFER_FEE`. [5](#0-4) 

The notification is then recorded as `NotifiedTopUp(Ok(...))` or `NotifiedMint(Ok(...))`, permanently preventing any retry of the burn: [6](#0-5) 

### Impact Explanation

When `burn_and_log` fails silently after cycles have been deposited:

- Cycles are created and credited to the caller (irreversible)
- The backing ICP remains unburned in the CMC's subaccount
- The ICP↔cycles conservation invariant is violated: cycles supply increases without a corresponding ICP supply decrease
- The ICP stays in the CMC's subaccount indefinitely, inaccessible to the original sender (the notification is finalized as success)

This is a **ledger conservation bug**: assets (cycles) are issued without the corresponding asset destruction (ICP burn), analogous to the external report's "Facilitator borrows assets and never repays."

### Likelihood Explanation

The ICP ledger (`send_pb`) call in `burn_and_log` can fail due to:

1. **Transient inter-canister call rejection** — the ICP ledger is a separate canister; any transient system error (subnet overload, message queue full) causes a silent burn failure
2. **`minting_account_id` not configured** — if CMC state is misconfigured, the burn is skipped entirely with only a log message
3. **`amount < DEFAULT_TRANSFER_FEE`** — in the `refund_icp` path, `extra_fee` values smaller than `DEFAULT_TRANSFER_FEE` cause a silent no-op burn

Scenario 1 is not directly attacker-controlled but is a realistic operational condition. Scenarios 2 and 3 are deterministic code paths reachable without any privileged access. Any unprivileged user who sends ICP to the CMC and calls `notify_top_up` or `notify_mint_cycles` participates in this flow.

### Recommendation

1. **Separate the burn from the success response**: Record the notification as pending-burn rather than immediately finalizing it as success. Only finalize after the burn is confirmed.
2. **Retry the burn**: If `burn_and_log` fails, store the pending burn in CMC state and retry it in a heartbeat or timer, rather than silently discarding the failure.
3. **Alert on burn failure**: At minimum, emit a critical-error metric (not just a log) when `burn_and_log` fails, so operators can manually recover the stuck ICP.

### Proof of Concept

1. User sends 10 ICP to `CMC_CANISTER_ID/<user_subaccount>` on the ICP ledger with memo `MEMO_TOP_UP_CANISTER`.
2. User calls `notify_top_up { block_index, canister_id }` on the CMC.
3. CMC calls `ensure_balance` → `ic0_mint_cycles128` → mints cycles.
4. CMC calls `deposit_cycles` → `call_with_payment128(management_canister, "deposit_cycles", ...)` → cycles deposited to `canister_id`. **Irreversible.**
5. CMC calls `burn_and_log` → `call_protobuf(ledger_canister_id, "send_pb", ...)`.
6. If the ICP ledger returns a transient error (e.g., `SysTransient`), `burn_and_log` logs the error and returns `()`.
7. CMC records `NotifiedTopUp(Ok(cycles))` — the notification is permanently finalized.
8. Result: `canister_id` received cycles; the 10 ICP remains unburned in the CMC's subaccount; cycles supply is inflated by the minted amount with no ICP backing. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1305-1312)
```rust
            with_state_mut(|state| {
                state.blocks_notified.insert(
                    block_index,
                    NotificationStatus::NotifiedMint(result.clone()),
                );
                if is_transient_error(&result) {
                    state.blocks_notified.remove(&block_index);
                }
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

**File:** rs/nns/cmc/src/main.rs (L2103-2105)
```rust
    if burned > Tokens::ZERO {
        burn_and_log(from_subaccount, burned).await;
    }
```

**File:** rs/nns/cmc/src/main.rs (L2110-2138)
```rust
async fn deposit_cycles(
    canister_id: CanisterId,
    cycles: Cycles,
    mint_cycles: bool,
    limiter_to_use: CyclesMintingLimiterSelector,
) -> Result<(), String> {
    if mint_cycles {
        ensure_balance(cycles, limiter_to_use)?;
    }

    let res: CallResult<()> = ic_cdk::api::call::call_with_payment128(
        candid::Principal::management_canister(),
        METHOD_DEPOSIT_CYCLES,
        (CanisterIdRecord {
            canister_id: canister_id.get().0,
        },),
        u128::from(cycles),
    )
    .await;

    res.map_err(|(code, msg)| {
        format!(
            "Depositing cycles failed with code {}: {:?}",
            code as i32, msg
        )
    })?;

    Ok(())
}
```
