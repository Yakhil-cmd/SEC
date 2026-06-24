### Title
Unchecked Ledger Burn Return Value in CMC `burn_and_log` Causes Permanent ICP Conservation Violation - (File: rs/nns/cmc/src/main.rs)

### Summary
The Cycles Minting Canister's `burn_and_log` function explicitly ignores the return value of the ICP ledger burn transfer. If the burn call fails (e.g., ledger temporarily unavailable during an upgrade), the ICP remains permanently locked in CMC's subaccount while the notification is already marked as processed and cannot be retried. This violates the invariant that cycles are backed by burned ICP.

### Finding Description
In `rs/nns/cmc/src/main.rs`, the `burn_and_log` function is responsible for burning ICP from CMC's subaccount after a successful canister creation, top-up, or cycle mint. The function calls the ICP ledger's `send_pb` endpoint and, on failure, only logs the error and returns `()`:

```rust
async fn burn_and_log(from_subaccount: Subaccount, amount: Tokens) {
    // ...
    let res: CallResult<BlockIndex> = call_protobuf(ledger_canister_id, "send_pb", send_args).await;
    match res {
        Ok(block) => print(format!("{msg} done in block {block}.")),
        Err((code, err)) => {
            let code = code as i32;
            print(format!("{msg} failed with code {code}: {err:?}"))
        }
    }
}
``` [1](#0-0) 

This function is called by `process_create_canister`, `process_top_up`, and `process_mint_cycles` **after** the main operation has already succeeded and the notification block status has been committed to a terminal state (`NotifiedCreateCanister`, `NotifiedTopUp`, `NotifiedMint`):

```rust
async fn process_create_canister(...) -> Result<CanisterId, NotifyError> {
    match do_create_canister(controller, cycles, subnet_selection, settings).await {
        Ok(canister_id) => {
            burn_and_log(sub, amount).await;  // result ignored
            Ok(canister_id)
        }
        ...
    }
}
``` [2](#0-1) 

Similarly for `process_top_up`: [3](#0-2) 

And `process_mint_cycles`: [4](#0-3) 

The notification block is set to `Processing` before the operation and then to a terminal status after it completes. Because the terminal status is set regardless of whether `burn_and_log` succeeds, there is no mechanism to retry the burn. The ICP that should have been burned remains in CMC's subaccount indefinitely.

The design comment in the code acknowledges this intentionally: *"Burning doesn't return errors - we don't want to reject the transaction notification because then it could be retried."* However, this design choice creates a permanent ledger conservation violation when the burn fails. [5](#0-4) 

### Impact Explanation
The ICP-to-cycles invariant is violated: cycles are minted (or a canister is created) but the corresponding ICP is not burned. The ICP remains permanently locked in CMC's subaccount with no recovery path, since:
1. The notification block is already in a terminal state and cannot be reprocessed.
2. `burn_and_log` has no retry mechanism and no persistent record of failed burns.
3. There is no administrative endpoint to retry a failed burn for a specific subaccount.

This is a **ledger conservation bug**: the total ICP supply is not reduced as expected, breaking the 1:1 backing of cycles by burned ICP. The magnitude scales with the amount of ICP involved in the failed notification.

### Likelihood Explanation
The ICP ledger can be temporarily unavailable during canister upgrades (which happen via NNS governance proposals on a regular basis). If `burn_and_log` is called during the brief window when the ledger is stopped or upgrading, the inter-canister call will fail with a system error. This is a realistic, non-adversarial scenario. An attacker who can time a `notify_*` call to coincide with a ledger upgrade window could also trigger this deliberately, though the primary attacker benefit is disrupting the ICP burn accounting rather than direct financial gain.

### Recommendation
Replace the fire-and-forget `burn_and_log` pattern with one of the following:
1. **Persistent retry queue**: On burn failure, store the pending burn (subaccount + amount) in stable state and retry it on the next heartbeat/timer tick until it succeeds.
2. **Propagate the error**: Return an error from `burn_and_log` and handle it in callers by either retrying or storing the failed burn for later recovery.
3. **Pre-burn before operation**: Burn the ICP before creating the canister/minting cycles, and refund on failure (as is already done in the refund path).

### Proof of Concept
1. User sends 10 ICP to CMC's subaccount derived from their principal.
2. User calls `notify_create_canister` with the corresponding block index.
3. CMC sets the block status to `Processing`, then calls `do_create_canister` — this succeeds and a canister is created.
4. CMC calls `burn_and_log(sub, amount)` to burn the 10 ICP.
5. At this moment, the ICP ledger is temporarily unavailable (e.g., mid-upgrade).
6. `call_protobuf(..., "send_pb", ...)` returns an error; `burn_and_log` logs it and returns `()`.
7. CMC returns `Ok(canister_id)` to the caller; the block status is set to `NotifiedCreateCanister`.
8. The 10 ICP remains in CMC's subaccount. The notification cannot be retried (block is in terminal state). The ICP is never burned.
9. Net result: user received a canister, 10 ICP was not burned, total ICP supply is 10 ICP higher than it should be.

### Citations

**File:** rs/nns/cmc/src/main.rs (L1943-1955)
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
```

**File:** rs/nns/cmc/src/main.rs (L1966-1982)
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
```

**File:** rs/nns/cmc/src/main.rs (L1999-2011)
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
```

**File:** rs/nns/cmc/src/main.rs (L2014-2016)
```rust
/// Attempt to burn the funds.
/// Burning doesn't return errors - we don't want to reject the transaction
/// notification because then it could be retried.
```

**File:** rs/nns/cmc/src/main.rs (L2017-2049)
```rust
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
