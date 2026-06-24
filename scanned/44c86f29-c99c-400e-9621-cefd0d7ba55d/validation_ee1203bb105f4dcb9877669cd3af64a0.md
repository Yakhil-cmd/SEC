### Title
Unchecked ICP Burn Result After Cycle Minting Breaks Conservation Invariant - (File: rs/nns/cmc/src/main.rs)

### Summary
In the Cycles Minting Canister (CMC), after successfully minting cycles via `process_mint_cycles` and `process_top_up`, the ICP burn step is performed by `burn_and_log(sub, amount).await` whose result is silently discarded. If the burn call to the ICP ledger fails, cycles are already minted/deposited but the corresponding ICP is never burned, breaking the ICP↔cycles conservation invariant.

### Finding Description
In `rs/nns/cmc/src/main.rs`, both `process_mint_cycles` and `process_top_up` follow the same pattern:

1. Mint/deposit cycles to the beneficiary (irreversible on-chain action).
2. Call `burn_and_log(sub, amount).await` to burn the ICP from the CMC subaccount.
3. Return `Ok(...)` regardless of whether step 2 succeeded.

The return value of `burn_and_log` is never inspected — no `?`, no `match`, no `let result =`. The function name implies it only logs on failure rather than propagating the error. [1](#0-0) 

```rust
// process_mint_cycles
Ok(deposit_result) => {
    burn_and_log(sub, amount).await;   // ← result discarded
    Ok(NotifyMintCyclesSuccess { ... })
}
``` [2](#0-1) 

```rust
// process_top_up
Ok(()) => {
    burn_and_log(sub, amount).await;   // ← result discarded
    Ok(cycles)
}
```

### Impact Explanation
If `burn_and_log` fails (e.g., the ICP ledger returns a transient error or is temporarily unavailable), cycles are already minted and deposited to the beneficiary, but the ICP in the CMC subaccount is never burned. This creates cycles out of thin air, violating the ICP↔cycles conservation invariant that underpins the economic model of the Internet Computer. Repeated occurrences inflate the total cycle supply without reducing ICP supply.

### Likelihood Explanation
The ICP ledger is a system canister and is generally highly available, but transient errors (e.g., `TemporarilyUnavailable`, reject codes from the execution environment, or canister queue overflow) are possible. Any unprivileged user who calls `notify_mint_cycles` or `notify_top_up` at a moment when the ledger is transiently unavailable after the cycle deposit succeeds would trigger this path. The window is narrow but real, and the CMC processes many notifications.

### Recommendation
Propagate the result of `burn_and_log`. If the burn fails, either:
- Reverse the cycle mint (if possible), or
- Record the failed burn in durable state and retry it, ensuring the ICP is eventually burned before the function returns success.

At minimum, if the burn fails the function should return an error rather than `Ok(...)`, so the notification can be retried by the caller and the block index is not marked as fully processed.

### Proof of Concept

1. User sends ICP to CMC subaccount with `MEMO_MINT_CYCLES`.
2. User calls `notify_mint_cycles` (or `notify_top_up`).
3. CMC calls `do_mint_cycles` (or `deposit_cycles`) — succeeds, cycles are deposited.
4. CMC calls `burn_and_log(sub, amount).await` — ICP ledger returns a transient error.
5. `burn_and_log` logs the error internally but returns `()`.
6. `process_mint_cycles` returns `Ok(NotifyMintCyclesSuccess { ... })`.
7. The block index is recorded as `NotificationStatus::NotifiedMint(Ok(...))` in `blocks_notified`, preventing any retry.
8. The ICP in the CMC subaccount is never burned; cycles exist without corresponding ICP destruction. [3](#0-2)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1302-1316)
```rust
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
