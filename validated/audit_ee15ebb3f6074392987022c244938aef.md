### Title
Unchecked Burn Transfer Return Value in CMC Allows Cycles Minting Without ICP Burn — (File: `rs/nns/cmc/src/main.rs`)

---

### Summary

The `burn_and_log` function in the Cycles Minting Canister (CMC) performs a ledger transfer to burn ICP but intentionally returns `()`, swallowing all errors. Its callers — `process_create_canister`, `process_top_up`, `process_mint_cycles`, and `refund_icp` — cannot observe or react to burn failures. When the burn fails, cycles have already been minted/deposited, the notification is marked as processed (preventing retries), and the ICP remains unburned in the CMC subaccount. This breaks the ICP/cycles economic conservation invariant.

---

### Finding Description

In `rs/nns/cmc/src/main.rs`, the `burn_and_log` function is defined as:

```rust
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

The function signature returns `()`. The design comment explicitly states: *"Burning doesn't return errors — we don't want to reject the transaction notification because then it could be retried."* [1](#0-0) 

All three primary callers invoke `burn_and_log` after cycles have already been minted or deposited:

- `process_create_canister` at line 1945: canister already created with cycles before burn
- `process_top_up` at line 2001: cycles already deposited before burn
- `process_mint_cycles` at line 1968: cycles already minted to cycles ledger before burn [2](#0-1) [3](#0-2) [4](#0-3) 

The `refund_icp` function also calls `burn_and_log` for the non-refunded portion: [5](#0-4) 

---

### Impact Explanation

**Vulnerability class: Ledger conservation bug / unchecked transfer return value.**

If the ICP ledger rejects or fails the burn call (e.g., transient unavailability, ledger trap, or amount edge case), the following state results:

1. Cycles are minted and deposited to the target canister — irreversible.
2. The notification block index is recorded as processed — preventing any retry.
3. The ICP remains in the CMC's subaccount — unburned.

This breaks the fundamental economic invariant of the IC: cycles should only be created by burning ICP. A failed burn means cycles exist without corresponding ICP destruction, inflating the effective cycles supply. The ICP is also permanently stranded in the CMC subaccount with no recovery path for the user.

---

### Likelihood Explanation

**Low-to-medium.** The ICP ledger is a highly available NNS canister, but transient inter-canister call failures are possible on the IC (e.g., queue full, canister stopping, subnet under load). The window is narrow — the failure must occur specifically at the `burn_and_log` call after cycles are already committed. No attacker control over the ledger is required; a naturally occurring transient failure is sufficient. The impact per occurrence is bounded to the amount of ICP in the specific CMC subacduct, but the invariant violation is permanent for that transaction.

---

### Recommendation

1. Change `burn_and_log` to return `Result<BlockIndex, ...>` and propagate the error to callers.
2. If the design intent is to never block notification processing on burn failures, implement a persistent retry queue: store failed burn amounts in stable state and retry them in the heartbeat, similar to how the CMC already handles other deferred operations.
3. At minimum, emit a metric or certified state flag when a burn fails so that the discrepancy between minted cycles and burned ICP can be detected and remediated by governance.

---

### Proof of Concept

1. User sends 10 ICP to the CMC subaccount keyed to their principal.
2. User calls `notify_top_up` (or `notify_create_canister` / `notify_mint_cycles`).
3. CMC successfully calls `deposit_cycles` — cycles are deposited to the target canister. This is irreversible.
4. CMC calls `burn_and_log(sub, amount).await` — the ICP ledger call fails (e.g., `call_protobuf` returns `Err`).
5. `burn_and_log` logs the error and returns `()`. The caller (`process_top_up`) returns `Ok(cycles)`.
6. The notification block index is written to `blocks_notified` as `NotifiedTopUp(Ok(...))`.
7. Any subsequent call to `notify_top_up` with the same block index returns the cached success result — no retry of the burn is possible.
8. Result: cycles exist in the target canister; 10 ICP remain unburned in the CMC subaccount; the ICP/cycles conservation invariant is violated. [6](#0-5)

### Citations

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

**File:** rs/nns/cmc/src/main.rs (L2103-2105)
```rust
    if burned > Tokens::ZERO {
        burn_and_log(from_subaccount, burned).await;
    }
```
