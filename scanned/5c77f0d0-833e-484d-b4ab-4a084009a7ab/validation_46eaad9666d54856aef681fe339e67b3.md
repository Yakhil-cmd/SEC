### Title
Unchecked Return Value of `burn_and_log` After Cycles Minting/Top-Up — (`File: rs/nns/cmc/src/main.rs`)

### Summary

In the Cycles Minting Canister (CMC), after successfully minting cycles or topping up a canister, the ICP burn step (`burn_and_log`) is awaited but its return value is never checked or propagated. If the burn call fails transiently, cycles are already deposited to the user while the corresponding ICP remains unburned, breaking the ICP↔cycles conservation invariant.

### Finding Description

The external report's vulnerability class is **unchecked return values from token transfer/burn functions**, where a successful operation is assumed without verifying the returned status. The IC analog is in the CMC's `process_mint_cycles` and `process_top_up` functions.

In both functions, after the cycles deposit succeeds, `burn_and_log` is called to burn the ICP from the CMC's subaccount on the ICP ledger. The call is awaited but the result is silently discarded — there is no `?`, no `match`, and no `let result =` binding:

```rust
// rs/nns/cmc/src/main.rs ~line 1968
Ok(deposit_result) => {
    burn_and_log(sub, amount).await;   // ← return value not checked
    Ok(NotifyMintCyclesSuccess { ... })
}
```

```rust
// rs/nns/cmc/src/main.rs ~line 2001
Ok(()) => {
    burn_and_log(sub, amount).await;   // ← return value not checked
    Ok(cycles)
}
``` [1](#0-0) [2](#0-1) 

The sequence is:
1. CMC calls `do_mint_cycles` / `deposit_cycles` → **cycles are deposited** (irreversible at this point).
2. CMC calls `burn_and_log(sub, amount).await` → **ICP burn is attempted but result is ignored**.
3. If the burn fails (e.g., transient ledger rejection), the function returns `Ok(...)` to the caller as if everything succeeded.

### Impact Explanation

**Ledger conservation bug.** If `burn_and_log` fails silently:
- The user receives cycles.
- The ICP in the CMC's subaccount is **not burned** from the ICP ledger.
- The ICP total supply is inflated relative to the cycles supply.
- The CMC's subaccount retains ICP that should have been destroyed, and a subsequent `notify_*` call on the same block index would be rejected as a duplicate — so the ICP is permanently stranded and unburned.

This breaks the fundamental economic invariant of the Internet Computer: every cycle must correspond to burned ICP.

### Likelihood Explanation

The ICP ledger is a system canister and is generally reliable, but transient rejections (e.g., output queue full, canister temporarily unavailable) are possible under load. The CMC is a high-value, frequently-called canister. An unprivileged user can trigger this path by:
1. Sending ICP to the CMC's subaccount (standard user action).
2. Calling `notify_mint_cycles` or `notify_top_up` (standard user action).
3. If the ledger is transiently unavailable at the moment `burn_and_log` executes, the bug fires.

No privileged access, no key compromise, and no majority attack is required.

### Recommendation

Propagate the result of `burn_and_log`. If the burn fails, either:
- Trap/panic to roll back the entire update (not possible here since cycles are already deposited via a prior inter-canister call that has committed), or
- Return an error to the caller and implement a retry/recovery mechanism (e.g., record the pending burn in stable state and retry in a heartbeat), or
- At minimum, log the failure and emit a metric so operators can detect and manually reconcile the discrepancy.

The safest fix is to restructure the flow so that the ICP burn is confirmed before cycles are deposited, or to use a two-phase commit pattern with stable-state journaling.

### Proof of Concept

1. User sends 1 ICP to `CMC_CANISTER_ID` subaccount derived from their principal, with memo `MEMO_MINT_CYCLES`.
2. User calls `notify_mint_cycles` on the CMC.
3. CMC calls `do_mint_cycles` → cycles ledger mints cycles to user. **Cycles are now in user's account.**
4. CMC calls `burn_and_log(sub, 1_ICP).await`. Suppose the ICP ledger rejects with `SysTransient` at this moment.
5. `burn_and_log` returns an `Err` (or logs and returns `()`). The CMC ignores it.
6. CMC returns `Ok(NotifyMintCyclesSuccess { ... })` to the user.
7. **Result:** User has cycles. ICP ledger total supply is 1 ICP higher than it should be. The CMC's subaccount still holds 1 ICP that was never burned. [3](#0-2) [4](#0-3)

### Citations

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
