### Title
Silent Burn Failure Leaves ICP Permanently Stuck in CMC Subaccount - (File: rs/nns/cmc/src/main.rs)

### Summary
The Cycles Minting Canister (CMC) intentionally ignores failures from the ICP burn step that follows a successful cycles deposit or mint. If the burn transfer to the minting account fails, the ICP remains permanently locked in the CMC's subaccount with no recovery path, because the notification is already recorded as successfully processed and cannot be retried.

### Finding Description
After a user calls `notify_top_up` or `notify_mint_cycles`, the CMC executes a two-step sequence:

1. Deposit cycles to the target canister / mint cycles to the cycles ledger (irreversible).
2. Burn the corresponding ICP from the CMC's subaccount by calling `burn_and_log`.

The `burn_and_log` function is explicitly designed to swallow errors:

```rust
// rs/nns/cmc/src/main.rs:2014-2049
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
            print(format!("{msg} failed with code {code}: {err:?}"))  // error only logged
        }
    }
}
``` [1](#0-0) 

This function is called unconditionally after a successful cycles operation:

```rust
// process_top_up (line 2001) and process_mint_cycles (line 1968)
Ok(()) => {
    burn_and_log(sub, amount).await;  // failure silently discarded
    Ok(cycles)
}
``` [2](#0-1) [3](#0-2) 

Before `burn_and_log` is called, the notification is already recorded as `NotifiedTopUp(Ok(cycles))` in `blocks_notified`:

```rust
// rs/nns/cmc/src/main.rs:1214-1221
with_state_mut(|state| {
    state.blocks_notified.insert(
        block_index,
        NotificationStatus::NotifiedTopUp(result.clone()),
    );
    ...
});
``` [4](#0-3) 

Any subsequent call to `notify_top_up` with the same `block_index` hits the deduplication path and returns the cached `Ok(cycles)` immediately, without re-attempting the burn:

```rust
NotificationStatus::NotifiedTopUp(result) => Some(result.clone()),
``` [5](#0-4) 

There is no admin endpoint, governance proposal path, or any other built-in mechanism to re-trigger the burn for a notification that was already recorded as successful.

### Impact Explanation
**Impact: High.** When `burn_and_log` fails, the ICP deposited into the CMC's subaccount (keyed by `Subaccount::from(&canister_id)` for top-ups, or `Subaccount::from(&TEST_USER1_PRINCIPAL)` for mint-cycles) is permanently inaccessible. The ICP ledger balance in that subaccount is non-zero but there is no code path that can move it out. This violates the ICP/cycles conservation invariant: cycles were minted but the corresponding ICP was not burned, leaving the total ICP supply higher than it should be. The stuck ICP accumulates over time with each failed burn.

### Likelihood Explanation
**Likelihood: Low.** The ICP ledger must return an error (e.g., `TemporarilyUnavailable`, a reject from the subnet, or a canister trap) at the exact moment `burn_and_log` calls `send_pb`. This can happen during ledger upgrades, subnet instability, or message queue exhaustion. It is not directly attacker-controllable but is a realistic operational scenario on a live network.

### Recommendation
Remove the silent-discard pattern. Instead of recording the notification as `NotifiedTopUp(Ok(cycles))` before the burn, record it only after both the cycles deposit **and** the burn succeed. If the burn fails, record the notification as a transient error (`NotifyError::Processing` or a new `BurnFailed` variant) so the caller can retry. Alternatively, implement a separate admin-callable sweep function that re-attempts pending burns for subaccounts with non-zero balances, similar to how `sweep_icp` works in the SNS swap canister. [6](#0-5) 

### Proof of Concept

1. User sends 10 ICP to `CMC_SUBACCOUNT(canister_X)` on the ICP ledger (block index `B`).
2. User calls `notify_top_up { block_index: B, canister_id: canister_X }`.
3. CMC calls `process_top_up` → `deposit_cycles` succeeds → `canister_X` receives cycles.
4. CMC records `blocks_notified[B] = NotifiedTopUp(Ok(cycles))`.
5. CMC calls `burn_and_log(subaccount_of_canister_X, 10 ICP)`.
6. The ICP ledger returns `Err(TemporarilyUnavailable)` (e.g., during a ledger upgrade).
7. `burn_and_log` logs the error and returns `()` — no error propagated.
8. The 10 ICP remains in `CMC_SUBACCOUNT(canister_X)` forever.
9. Any retry of `notify_top_up { block_index: B, ... }` returns the cached `Ok(cycles)` immediately without re-attempting the burn.
10. No NNS governance proposal can recover the stuck ICP without a CMC code upgrade that adds a recovery function. [7](#0-6)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1186-1188)
```rust
                // the current request is the original one.
                NotificationStatus::NotifiedTopUp(result) => Some(result.clone()),
                NotificationStatus::NotifiedCreateCanister(_) => {
```

**File:** rs/nns/cmc/src/main.rs (L1212-1226)
```rust
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

**File:** rs/nns/cmc/src/main.rs (L1966-1983)
```rust
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
