### Title
Silently Ignored Ledger Burn Failure Causes ICP Supply Conservation Bug - (File: rs/nns/cmc/src/main.rs)

### Summary
The Cycles Minting Canister (CMC) contains a `burn_and_log` function that intentionally swallows errors from the ICP ledger burn call. After successfully dispensing cycles or creating a canister, the CMC calls `burn_and_log` to remove the corresponding ICP from circulation. If this ledger call fails, the error is only logged — execution continues as if the burn succeeded. The ICP tokens remain unburned in the CMC's subaccount while cycles have already been dispensed, creating an ICP supply conservation bug.

### Finding Description
In `rs/nns/cmc/src/main.rs`, the function `burn_and_log` is called after successful canister creation, cycle top-up, and cycle minting operations. The function makes an inter-canister call to the ICP ledger's `send_pb` method to burn the ICP tokens. The result is matched, but the `Err` branch only logs the failure and returns — it does not propagate the error to callers. [1](#0-0) 

The callers — `process_create_canister`, `process_top_up`, and `process_mint_cycles` — all call `burn_and_log(...).await` and then return `Ok(...)` regardless of whether the burn succeeded: [2](#0-1) [3](#0-2) 

The code comment explicitly acknowledges this design: *"Burning doesn't return errors - we don't want to reject the transaction notification because then it could be retried."* This means the error is intentionally swallowed to prevent double-spending of cycles, but at the cost of silently failing to burn ICP. [4](#0-3) 

### Impact Explanation
When `burn_and_log` fails silently:
1. The CMC has already dispensed cycles or created a canister (value has left the system).
2. The ICP tokens that should have been burned remain in the CMC's subaccount.
3. The notification block is marked as processed (`NotifiedCreateCanister`, `NotifiedTopUp`, or `NotifiedMint`), so it cannot be retried.
4. The ICP supply is not reduced as intended — tokens that should be destroyed remain in circulation, violating the ICP/cycles conservation invariant.

This is a **ledger conservation bug**: the CMC has issued cycles backed by ICP that was never burned, inflating the effective ICP supply relative to the cycles in circulation.

### Likelihood Explanation
The `burn_and_log` failure path is reachable whenever the ICP ledger canister is temporarily unavailable (e.g., during subnet upgrades, under heavy load, or due to transient message routing failures). No privileged access or attacker control is required — any transient inter-canister call failure to the ledger triggers this path. Given that the CMC processes many notifications, the probability of at least one such failure over time is non-negligible.

### Recommendation
Propagate the burn error to callers instead of swallowing it. If the concern is preventing retries, the notification status should be set to a terminal "burned-failed" state that records the dispensed value but flags the burn as incomplete, allowing an operator-triggered reconciliation path. Alternatively, the burn should be retried with idempotency (using `created_at_time` deduplication on the ledger) before marking the notification as complete.

### Proof of Concept
1. User sends ICP to CMC subaccount and calls `notify_top_up`.
2. CMC calls `process_top_up`, which calls `deposit_cycles` successfully — cycles are added to the target canister.
3. CMC then calls `burn_and_log` to burn the ICP from its subaccount.
4. The ICP ledger is temporarily unavailable (e.g., mid-upgrade); `call_protobuf` returns `Err(...)`.
5. `burn_and_log` logs the error and returns `()` — no error is propagated.
6. `process_top_up` returns `Ok(cycles)` to the caller.
7. The notification is recorded as `NotifiedTopUp(Ok(cycles))` — permanently processed.
8. The ICP tokens remain in the CMC's subaccount, unburned, while cycles have been dispensed. [5](#0-4)

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
