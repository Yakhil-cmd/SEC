### Title
Silent Burn Failure in Cycles Minting Canister Allows ICP Conservation Violation - (File: rs/nns/cmc/src/main.rs)

### Summary
The Cycles Minting Canister (CMC) contains a `burn_and_log` function that silently swallows ledger burn failures. After successfully delivering cycles or creating a canister, the CMC attempts to burn the deposited ICP from its subaccount. If the ledger call fails for any reason (e.g., temporary unavailability, insufficient balance due to rounding, or a reject), the error is only logged and execution continues. The ICP is never burned, violating ledger conservation: the total ICP supply is not reduced even though cycles or a canister were dispensed.

### Finding Description
The `burn_and_log` function is called unconditionally after three successful CMC operations:

- `process_create_canister` — after `do_create_canister` succeeds
- `process_top_up` — after `deposit_cycles` succeeds
- `process_mint_cycles` — after `do_mint_cycles` succeeds [1](#0-0) 

The function calls the ICP ledger's `send_pb` endpoint to burn the ICP held in the CMC's per-user subaccount. On failure it only prints a log message and returns `()`: [2](#0-1) 

The callers treat the return value of `burn_and_log` as fire-and-forget: [3](#0-2) [4](#0-3) [5](#0-4) 

The design comment acknowledges this intentionally:

> "Burning doesn't return errors — we don't want to reject the transaction notification because then it could be retried."

However, this design choice means that whenever the ICP ledger rejects or fails the burn call (e.g., `TemporarilyUnavailable`, `InsufficientFunds` due to a fee edge case, or a canister-level reject), the ICP remains in the CMC's subaccount unburned while the cycles or canister have already been dispensed.

The analog to the ERC-20 report is direct: just as `transferFrom` returning `false` instead of reverting allowed a deposit to be recorded without actual token movement, `burn_and_log` returning silently on ledger error allows cycles/canisters to be dispensed without the corresponding ICP destruction.

### Impact Explanation
**Ledger conservation violation**: ICP that should be destroyed (burned to the minting account) remains in circulation inside the CMC's subaccount. Over time, repeated burn failures accumulate unburned ICP, inflating the effective circulating supply relative to the cycles minted. The ICP is not returned to the user (the CMC's subaccount is not user-controlled), but it is also not destroyed, breaking the invariant that `cycles_minted ∝ ICP_burned`.

Additionally, the stuck ICP in the CMC's subaccount could be re-used in a future notification for the same subaccount if the user sends more ICP to the same CMC subaccount, potentially allowing the CMC to process a new notification with a combined balance that includes the previously unburned ICP — effectively giving the user a discount on a subsequent operation.

### Likelihood Explanation
The ICP ledger can return `TemporarilyUnavailable` during upgrades or under load. The CMC's `burn_and_log` also has an explicit early-return guard: [6](#0-5) 

If `amount < DEFAULT_TRANSFER_FEE`, the burn is silently skipped entirely — no ledger call is made and no ICP is burned. This is reachable whenever the CMC's subaccount balance is below the transfer fee (e.g., due to fee deductions in prior refund operations). An unprivileged user who sends exactly the minimum ICP required for a top-up or canister creation can trigger this path deterministically.

### Recommendation
1. **Retry on failure**: If `burn_and_log` fails, schedule a retry via a timer rather than silently dropping the error.
2. **Track unburned amounts**: Persist failed burn amounts in stable state and retry them in a background task, similar to how ckBTC and ckETH handle failed mints with `QuarantinedDeposit` guards.
3. **Propagate errors where safe**: For `process_mint_cycles` and `process_top_up`, consider propagating burn failures as a warning in the response without rejecting the notification, so operators can detect and remediate.
4. **Remove the silent early-return for small amounts**: The `amount < DEFAULT_TRANSFER_FEE` guard silently skips the burn; this should at minimum be tracked in state.

### Proof of Concept
1. User sends exactly `DEFAULT_TRANSFER_FEE - 1` e8s worth of ICP to the CMC's top-up subaccount for a canister (this is the minimum that passes the CMC's own checks after fee deduction).
2. User calls `notify_top_up`. CMC calls `deposit_cycles` successfully, dispensing cycles.
3. CMC calls `burn_and_log(sub, amount)` where `amount < DEFAULT_TRANSFER_FEE`.
4. `burn_and_log` hits the early-return at line 2027–2030 and returns without calling the ledger.
5. ICP is not burned. Cycles are dispensed. Ledger conservation is violated. [7](#0-6)

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
