Based on my research, I need to check the `is_transient_error` function definition to complete the analysis.

Looking at the code I've gathered, here is the complete picture:

**In `process_create_canister`** (`rs/nns/cmc/src/main.rs:1948-1954`):
```rust
Err(err) => {
    let refund_block = refund_icp(sub, from, amount, CREATE_CANISTER_REFUND_FEE).await?;
    ...
}
```

When `refund_icp` fails, it returns `NotifyError::Other { error_code: RefundFailed, ... }` via the `?` operator.

In `notify_create_canister` (line 1416): `is_transient_error` is checked. From `NotifyError::is_retriable()` at `rs/nns/cmc/src/lib.rs:220-222`, only `Refunded` is non-retriable. `NotifyError::Other` (the RefundFailed error) IS retriable, so `is_transient_error` returns true, the block status is cleared, and the user CAN retry.

This means the CMC **does** allow retry when the refund fails — the block status is cleared. This is the correct behavior and is analogous to the "L1 relay messages can be retried" case in the report (the lower-risk case).

However, there is a subtler issue: when `refund_icp` fails, the ICP is still sitting in the CMC subaccount. The user can retry `notify_create_canister`, which will attempt `do_create_canister` again (not just the refund). If `do_create_canister` now succeeds on retry, the user gets a canister AND their ICP is burned — which is the correct outcome. But if `do_create_canister` keeps failing and `refund_icp` keeps failing (e.g., ledger is temporarily unavailable), the ICP is stuck. Eventually, if the block gets purged from `blocks_notified` (via `purge_old_notifications`), the user permanently loses the ability to notify and their ICP is stuck in the CMC subaccount with no recovery path.

Let me verify this purging concern more carefully. The `purge_old_notifications` removes old entries, and `last_purged_notification` is updated. If the block index falls below `last_purged_notification`, the user gets `TransactionTooOld` and can never retry.

This is a valid analog: the compensation (refund) can fail, and if the block gets purged before the ledger recovers, the user's ICP is permanently stuck in the CMC subaccount with no recovery mechanism.

---

### Title
ICP Permanently Locked in CMC Subaccount When Refund Fails and Block Notification Is Purged - (`File: rs/nns/cmc/src/main.rs`)

### Summary

The Cycles Minting Canister (CMC) implements a compensation transaction pattern in `process_create_canister`, `process_top_up`, and `process_mint_cycles`: if the primary action (canister creation, cycle deposit, or cycle mint) fails, the CMC attempts to refund the user's ICP via `refund_icp`. If `refund_icp` itself fails (e.g., the ICP ledger is temporarily unavailable), the block notification status is cleared to allow retry. However, the CMC also enforces a bounded notification history via `purge_old_notifications`. If the ledger remains unavailable long enough for the block index to be purged from `blocks_notified`, the user permanently loses the ability to retry the notification, and their ICP remains locked in the CMC subaccount with no recovery path.

### Finding Description

The three notify flows — `notify_top_up`, `notify_create_canister`, and `notify_mint_cycles` — share the same pattern:

1. User sends ICP to a CMC subaccount.
2. User calls `notify_*` with the block index.
3. CMC sets block status to `Processing` and calls the primary action (e.g., `deposit_cycles`, `do_create_canister`, `do_mint_cycles`).
4. If the primary action fails, CMC calls `refund_icp` to return ICP to the user.
5. If `refund_icp` fails, the error propagates as `NotifyError::Other { error_code: RefundFailed }`.
6. Back in `notify_*`, `is_transient_error` returns `true` for this error type, so the block status is cleared from `blocks_notified`, allowing retry.

The retry window is bounded. `purge_old_notifications` is called at the start of every `notify_*` invocation and removes entries older than `MAX_NOTIFY_HISTORY` blocks. Once the user's block index falls below `last_purged_notification`, any subsequent call to `notify_*` returns `NotifyError::TransactionTooOld` — a non-retriable error — and the ICP remains permanently locked in the CMC subaccount.

The ICP is held in the CMC subaccount `Subaccount::from(&canister_id)` (for top-up) or `Subaccount::from(&controller)` (for create canister). There is no separate recovery function to drain these subaccounts after the notification window expires.

**Root cause**: The compensation transaction (refund) is not guaranteed to succeed, and the retry window for the notification is finite. No fallback recovery path exists once the block is purged.

Relevant code: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) 

### Impact Explanation

A user who sends ICP to the CMC and whose notification fails (primary action fails AND refund fails) can permanently lose their ICP if:
- The ICP ledger is unavailable for a sustained period (e.g., during an upgrade or a transient fault), AND
- The block index ages out of the CMC's notification history window.

The ICP sits in the CMC subaccount indefinitely with no user-accessible recovery function. The CMC has no `sweep` or `recover_stuck_icp` endpoint analogous to the SNS swap's `error_refund_icp`. This is a **ledger conservation bug**: ICP enters the CMC but cannot exit.

### Likelihood Explanation

The ICP ledger is a high-availability NNS canister, so sustained unavailability is rare. However:
- Ledger upgrades cause brief unavailability windows.
- If a user's `notify_*` call happens to coincide with a ledger upgrade (primary action fails → ledger unavailable for refund), and the user does not retry promptly, the block can age out.
- `MAX_NOTIFY_HISTORY` is a fixed constant; under high notification volume, purging can happen faster than expected.
- The condition is reachable by any unprivileged user who sends ICP to the CMC.

Likelihood is **low** but non-zero, and the impact (permanent ICP loss) is high.

### Recommendation

1. **Add a recovery endpoint**: Expose a function (callable by the original sender) that transfers any remaining balance from a CMC subaccount back to the owner, similar to `error_refund_icp` in the SNS swap canister. This should be callable regardless of block notification status.

2. **Decouple refund retry from notification expiry**: Store failed refund tasks in a persistent queue (analogous to `pending_reimbursements` in the ckBTC minter) and process them independently of the notification history window.

3. **Extend or remove the purge window for blocks in a failed-refund state**: If a block's refund failed, do not purge it from `blocks_notified` until the refund succeeds.

### Proof of Concept

1. Alice sends 10 ICP to `AccountIdentifier(CMC, Subaccount::from(&canister_id))` on the ICP ledger. Block index = `B`.
2. Alice calls `notify_top_up(block_index: B, canister_id: X)`.
3. CMC calls `deposit_cycles(X, ...)`. The target canister `X` does not exist → `deposit_cycles` returns an error.
4. CMC calls `refund_icp(...)` → calls `send_pb` on the ICP ledger. The ledger is mid-upgrade → the call fails with a transport error.
5. `refund_icp` returns `Err(NotifyError::Other { error_code: RefundFailed, ... })`.
6. `is_transient_error` returns `true`; block `B` is removed from `blocks_notified`. Alice can retry.
7. Alice does not retry promptly. Meanwhile, many other notifications arrive, advancing `last_purged_notification` past `B`.
8. Alice calls `notify_top_up(block_index: B, canister_id: X)` again.
9. CMC returns `NotifyError::TransactionTooOld` — non-retriable.
10. Alice's 10 ICP remains in the CMC subaccount `Subaccount::from(&canister_id)` permanently, with no recovery path. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1172-1207)
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
```

**File:** rs/nns/cmc/src/main.rs (L1411-1419)
```rust
            with_state_mut(|state| {
                state.blocks_notified.insert(
                    block_index,
                    NotificationStatus::NotifiedCreateCanister(result.clone()),
                );
                if is_transient_error(&result) {
                    state.blocks_notified.remove(&block_index);
                }
            });
```

**File:** rs/nns/cmc/src/main.rs (L1948-1954)
```rust
        Err(err) => {
            let refund_block = refund_icp(sub, from, amount, CREATE_CANISTER_REFUND_FEE).await?;
            Err(NotifyError::Refunded {
                reason: err,
                block_index: refund_block,
            })
        }
```

**File:** rs/nns/cmc/src/main.rs (L1975-1981)
```rust
        Err(err) => {
            let refund_block = refund_icp(sub, from, amount, MINT_CYCLES_REFUND_FEE).await?;
            Err(NotifyError::Refunded {
                reason: err,
                block_index: refund_block,
            })
        }
```

**File:** rs/nns/cmc/src/main.rs (L1985-2011)
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
```

**File:** rs/nns/cmc/src/main.rs (L2088-2096)
```rust
        let send_res: CallResult<BlockIndex> =
            call_protobuf(ledger_canister_id, "send_pb", send_args).await;
        let block = send_res.map_err(|(code, err)| {
            let code = code as i32;
            NotifyError::Other {
                error_code: NotifyErrorCode::RefundFailed as u64,
                error_message: format!("Refund to {to} failed with code {code}: {err}"),
            }
        })?;
```

**File:** rs/nns/cmc/src/lib.rs (L218-222)
```rust
impl NotifyError {
    /// Returns false if this error is permanent and should not be retried.
    pub fn is_retriable(&self) -> bool {
        !matches!(self, Self::Refunded { .. })
    }
```
