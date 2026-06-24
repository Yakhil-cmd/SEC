### Title
Silent ICP Burn Failure in Cycles Minting Canister Allows Cycles/Canister Delivery Without Burning ICP - (File: rs/nns/cmc/src/main.rs)

---

### Summary
The `burn_and_log` function in the Cycles Minting Canister (CMC) silently swallows ICP burn failures, returning `()` regardless of whether the ledger burn succeeded. Callers that deliver cycles or create canisters cannot detect burn failure. When the ICP ledger is temporarily unavailable at the burn step, ICP that should be destroyed to back minted cycles is not burned, creating a permanent ICP/cycles conservation discrepancy with no retry path.

---

### Finding Description

In `rs/nns/cmc/src/main.rs`, the async function `burn_and_log` is responsible for destroying the ICP held in the CMC's subaccount after a successful cycles-minting, canister-creation, or top-up operation. Its design explicitly swallows burn errors:

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
            print(format!("{msg} failed with code {code}: {err:?}"))
        }
    }
}
```

The function returns `()` in both the success and failure branches. Its three callers cannot distinguish success from failure:

- `process_create_canister` (line 1945): `burn_and_log(sub, amount).await;`
- `process_mint_cycles` (line 1968): `burn_and_log(sub, amount).await;`
- `process_top_up` (line 2001): `burn_and_log(sub, amount).await;` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

The sequence of events when a burn fails silently:

1. User calls `notify_top_up` / `notify_create_canister` / `notify_mint_cycles` (public ingress endpoints).
2. CMC marks the block as `Processing`, then performs the cycles/canister operation successfully.
3. CMC marks the block as `NotifiedTopUp` / `NotifiedCreateCanister` / `NotifiedMint` — the notification is now **permanently consumed** and cannot be retried.
4. CMC calls `burn_and_log`; the ICP ledger call fails (e.g., ledger temporarily unavailable during upgrade).
5. The error is only printed; `burn_and_log` returns `()`.
6. The ICP remains in the CMC's subaccount, unburned, with no retry mechanism. [5](#0-4) 

---

### Impact Explanation

This is a **ledger conservation bug**. ICP is the backing asset for cycles on the Internet Computer: cycles are minted by burning ICP at the current XDR conversion rate. When `burn_and_log` fails silently:

- Cycles (or a new canister with cycles) have been delivered to the user.
- The corresponding ICP has **not** been burned from the ledger.
- The ICP is stranded in the CMC's subaccount with no automated recovery path.
- The total ICP supply is higher than it should be relative to the cycles in circulation, violating the conservation invariant that underpins the ICP/cycles economy.

Over repeated occurrences (e.g., during ledger upgrades), this discrepancy accumulates silently.

---

### Likelihood Explanation

The ICP ledger is a separate canister. It is temporarily unavailable during canister upgrades, which happen periodically via NNS governance proposals. Any `notify_*` call that completes its cycles/canister delivery step during a ledger upgrade window will trigger this silent failure. No attacker action is required beyond submitting a normal `notify_top_up` / `notify_create_canister` / `notify_mint_cycles` ingress message at a time when the ledger is being upgraded. The entry path is fully unprivileged.

---

### Recommendation

`burn_and_log` should return a `Result` and its callers should handle failure. If the design intent is to never fail the notification (to prevent double-spending of cycles), the CMC should instead record the failed burn in durable state and retry it asynchronously in a heartbeat or timer, similar to how `blocks_notified` tracks notification status. At minimum, a failed burn should be recorded so that operators can detect and remediate the conservation discrepancy.

---

### Proof of Concept

1. User sends ICP to the CMC subaccount for canister top-up.
2. User calls `notify_top_up` via ingress.
3. CMC successfully deposits cycles into the target canister.
4. CMC marks the block as `NotifiedTopUp`.
5. CMC calls `burn_and_log`; the ICP ledger rejects the call (e.g., `CanisterStopping` during upgrade).
6. `burn_and_log` prints the error and returns `()`.
7. `notify_top_up` returns `Ok(cycles)` to the user.
8. The ICP ledger shows the CMC's subaccount still holds the original ICP amount.
9. A second call to `notify_top_up` with the same block index returns `NotifiedTopUp(Ok(cycles))` from cache — no second burn is attempted.
10. The ICP is permanently unburned; the cycles exist without backing. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1172-1227)
```rust
    let maybe_early_result = with_state_mut(|state| {
        state.purge_old_notifications(MAX_NOTIFY_HISTORY);

        if block_index <= state.last_purged_notification {
            return Some(Err(NotifyError::TransactionTooOld(
                state.last_purged_notification + 1,
            )));
        }

        match state.blocks_notified.entry(block_index) {
            Entry::Occupied(entry) => match entry.get() {
                NotificationStatus::Processing => Some(Err(NotifyError::Processing)),

                // If the user makes a duplicate request, we respond as though
                // the current request is the original one.
                NotificationStatus::NotifiedTopUp(result) => Some(result.clone()),
                NotificationStatus::NotifiedCreateCanister(_) => {
                    Some(Err(NotifyError::InvalidTransaction(
                        "The same payment is already processed as create canister request".into(),
                    )))
                }
                NotificationStatus::NotifiedMint(_) => Some(Err(NotifyError::InvalidTransaction(
                    "The same payment is already processed as mint request".into(),
                ))),
                NotificationStatus::NotMeaningfulMemo(_) => {
                    Some(Err(NotifyError::InvalidTransaction(
                        "The same payment is already processed as automatic refund".into(),
                    )))
                }
            },
            Entry::Vacant(entry) => {
                entry.insert(NotificationStatus::Processing);
                None
            }
        }
    });

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
}
```

**File:** rs/nns/cmc/src/main.rs (L1943-1956)
```rust
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

**File:** rs/nns/cmc/src/main.rs (L1999-2012)
```rust
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
