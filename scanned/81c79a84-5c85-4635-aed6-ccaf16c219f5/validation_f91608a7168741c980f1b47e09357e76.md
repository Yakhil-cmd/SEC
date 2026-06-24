### Title
Silent Burn Failure Causes ICP Supply Conservation Violation in Cycles Minting Canister - (File: rs/nns/cmc/src/main.rs)

### Summary
The `burn_and_log` function in the Cycles Minting Canister (CMC) intentionally discards the return value of the ICP ledger burn call. If the burn fails after a service (canister creation, cycles minting, or top-up) has already been delivered, the ICP remains unburned in the CMC's subaccount, inflating the effective ICP supply without any recovery mechanism.

### Finding Description
In `rs/nns/cmc/src/main.rs`, the function `burn_and_log` is called after each successful CMC operation to burn the user's ICP payment. The function explicitly ignores the ledger call result and only logs the outcome:

```rust
// rs/nns/cmc/src/main.rs ~line 2014
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

This function is called in three critical paths, all after the service has already been irreversibly delivered:

- `process_create_canister` (line 1945): after canister is created
- `process_mint_cycles` (line 1968): after cycles are minted to the cycles ledger
- `process_top_up` (line 2001): after cycles are deposited to a canister

In all three cases, the notification block is subsequently marked as processed (`NotifiedCreateCanister`, `NotifiedMint`, `NotifiedTopUp`), preventing any retry. If `burn_and_log` fails, the ICP stays in the CMC's subaccount permanently with no recovery path.

The analog to the Solidity report is direct: just as `transfer`/`transferFrom` return values were not checked in WstETH.sol, the return value of the ICP ledger burn call is not checked here — the difference being that in Rust the design is explicit (the function signature returns `()` and the error is swallowed by design).

### Impact Explanation
When `burn_and_log` fails silently:
1. The user has received their service (canister, cycles, or top-up).
2. The ICP payment is **not burned** — it remains in the CMC's subaccount keyed by the user's principal/canister subaccount.
3. The ICP ledger's total supply is not reduced as the protocol requires.
4. The stranded ICP cannot be recovered or re-burned without a governance upgrade, since the notification block is already marked processed.

This is a **ledger conservation bug**: ICP that should be destroyed remains in circulation (locked in the CMC), violating the invariant that ICP paid for CMC services is burned.

### Likelihood Explanation
The trigger requires a transient ICP ledger call failure (e.g., a reject from the ledger canister, a timeout, or a transient system error). While an unprivileged user cannot directly force the ledger to reject the burn, the CMC's own comment acknowledges this failure mode is possible and was consciously traded off against retryability. Any transient ledger unavailability during the burn window — which occurs after the service is delivered — silently produces the conservation violation. The likelihood is **low** for any single transaction but non-zero over the lifetime of the CMC given the volume of operations.

### Recommendation
`burn_and_log` should be changed to return a `Result` and its callers should handle the error. One approach is to record a "pending burn" entry in CMC state when the burn fails, and retry it in the heartbeat. This preserves the non-retryability of the notification while ensuring the burn eventually succeeds:

```rust
async fn burn_and_log(from_subaccount: Subaccount, amount: Tokens) -> Result<BlockIndex, String> {
    ...
    call_protobuf(ledger_canister_id, "send_pb", send_args)
        .await
        .map_err(|(code, err)| format!("burn failed with code {code}: {err}"))
}
```

Callers should then enqueue a retry on failure rather than silently proceeding.

### Proof of Concept
1. User sends ICP to the CMC's subaccount and calls `notify_top_up`.
2. CMC calls `deposit_cycles` — succeeds, cycles are deposited.
3. CMC calls `burn_and_log` — the ICP ledger returns a transient error (e.g., `SYS_TRANSIENT`).
4. `burn_and_log` logs the error and returns `()`.
5. CMC marks the block as `NotifiedTopUp` — the notification cannot be retried.
6. The ICP remains in the CMC's subaccount. The ICP ledger total supply is higher than the protocol invariant requires.

Relevant code locations: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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
