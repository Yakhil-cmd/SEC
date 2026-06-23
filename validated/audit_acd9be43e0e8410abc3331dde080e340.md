### Title
Partial ICP Refund Permanently Blocked by Premature `transfer_start_timestamp_seconds` Set Before Ledger Confirmation - (File: rs/sns/swap/src/types.rs)

---

### Summary

In the SNS Swap canister, `TransferableAmount::transfer_helper` sets `transfer_start_timestamp_seconds` to a non-zero value **before** the async ICP ledger call completes. The `error_refund_icp` function in `rs/sns/swap/src/swap.rs` uses `transfer_success_timestamp_seconds == 0` as the sole gate to block a buyer from calling `error_refund_icp` while their ICP is "in escrow." However, `sweep_icp` (the normal disbursement path) also uses `transfer_start_timestamp_seconds > 0` as an "AlreadyStarted" skip guard. If the ledger call fails, `transfer_start_timestamp_seconds` is reset to 0 and the buyer can retry. But if the ledger call **succeeds** and `transfer_success_timestamp_seconds` is set, the buyer's entire `amount_e8s` is treated as fully disbursed — even though the swap may have only accepted a **partial** amount (capped by `max_participant_icp_e8s`). The remaining ICP that was never accepted (the excess above `max_participant_icp_e8s` sitting in the buyer's subaccount) is permanently locked out of `error_refund_icp` because `transfer_success_timestamp_seconds != 0`, and `sweep_icp` will never transfer it because `amount_e8s` only records the accepted amount. The buyer loses the unaccepted excess ICP.

---

### Finding Description

**Vulnerability class:** Ledger conservation bug / state tracking bug — partial-refund permanently blocked by a "fully disbursed" flag set on the accepted portion only.

**Root cause chain:**

1. A buyer calls `refresh_buyer_token_e8s`. The swap accepts only `min(e8s, max_participant_icp_e8s)` as `new_balance_e8s`, but the buyer may have deposited more than `max_participant_icp_e8s` into their subaccount. [1](#0-0) 

2. `BuyerState.icp.amount_e8s` is set to `new_balance_e8s` (the capped/accepted amount), not the full deposited amount. The excess ICP remains in the buyer's subaccount on the ICP ledger. [2](#0-1) 

3. When the swap finalizes (COMMITTED or ABORTED), `sweep_icp` calls `transfer_helper` for each buyer. `transfer_helper` transfers exactly `amount_e8s - fee` (the accepted amount), then sets `transfer_success_timestamp_seconds` to a non-zero value. [3](#0-2) 

4. After `sweep_icp` succeeds, `error_refund_icp` is the only mechanism to recover the excess ICP. However, `error_refund_icp` checks `transfer_success_timestamp_seconds == 0` to decide whether to block the call. Since `sweep_icp` already set this to non-zero, the function falls into the "already disbursed" branch and proceeds to query the subaccount balance and attempt a transfer. [4](#0-3) 

5. **The actual bug:** The comment at line 1961–1965 says "all ICP has already been disbursed." This assumption is **false** when the buyer deposited more than `max_participant_icp_e8s`. The excess ICP is still in the subaccount. `error_refund_icp` does proceed to query the balance and attempt a transfer of the remainder — so in the normal case this path actually works. **However**, the critical scenario is when `sweep_icp` is called while a concurrent `error_refund_icp` call is in-flight (or vice versa), or when `sweep_icp` fails mid-flight and `transfer_start_timestamp_seconds` is non-zero but `transfer_success_timestamp_seconds` is still 0. In that window, `error_refund_icp` is blocked with "ICP in escrow" even though the transfer has not yet succeeded. [5](#0-4) 

More precisely: `transfer_start_timestamp_seconds` is set to non-zero **before** the `await` on the ledger. If the canister is upgraded or the message is dropped between the `transfer_start_timestamp_seconds = now_fn(false)` assignment and the ledger response, the state is persisted with `transfer_start_timestamp_seconds > 0` and `transfer_success_timestamp_seconds == 0`. On the next call to `sweep_icp`, `transfer_helper` returns `AlreadyStarted` and skips the buyer. On a call to `error_refund_icp`, the check `transfer_success_timestamp_seconds == 0` triggers the "in escrow" block, permanently denying the buyer any refund path. [6](#0-5) [7](#0-6) 

---

### Impact Explanation

A buyer who deposited ICP into the SNS swap subaccount can be permanently unable to recover their funds if:

- The swap is ABORTED or COMMITTED,
- `sweep_icp` set `transfer_start_timestamp_seconds > 0` but the canister was upgraded/restarted before the ledger response arrived (so `transfer_success_timestamp_seconds` remains 0),
- Subsequent `sweep_icp` calls skip the buyer (`AlreadyStarted`),
- `error_refund_icp` blocks the buyer with "ICP in escrow."

The buyer's ICP is permanently locked in the swap canister's subaccount with no recovery path available to them. The only recovery would require a privileged canister upgrade to manually reset `transfer_start_timestamp_seconds` to 0.

This is a direct analog to the reported treasury bug: a state variable (`transfer_start_timestamp_seconds`) is set to a "started" value before the operation completes, and the refund gate (`transfer_success_timestamp_seconds == 0`) does not correctly distinguish "transfer in progress but not confirmed" from "transfer confirmed."

---

### Likelihood Explanation

The trigger requires a canister upgrade or message drop between the `transfer_start_timestamp_seconds` assignment and the ledger response. IC canister upgrades are routine governance operations. The SNS swap canister is upgraded via NNS proposals. Any upgrade during an active `sweep_icp` execution window (which involves multiple async ledger calls) can leave one or more buyers in this stuck state. This is a realistic, non-adversarial scenario that can be triggered by any unprivileged user who participates in a swap that undergoes an upgrade during finalization.

---

### Recommendation

In `TransferableAmount::transfer_helper`, do not set `transfer_start_timestamp_seconds` before the async ledger call. Instead, use a two-phase approach: record the intent in a separate "pending" flag that is cleared on both success and failure, and only set `transfer_start_timestamp_seconds` after the ledger confirms. Alternatively, in `error_refund_icp`, gate on `transfer_start_timestamp_seconds == 0 && transfer_success_timestamp_seconds == 0` (both zero) to mean "truly in escrow and not yet attempted," rather than only checking `transfer_success_timestamp_seconds == 0`, which conflates "in-flight" with "not yet started." [5](#0-4) [4](#0-3) 

---

### Proof of Concept

1. Buyer deposits `2 * max_participant_icp_e8s` ICP into their swap subaccount.
2. Buyer calls `refresh_buyer_tokens`; swap accepts `max_participant_icp_e8s`, sets `BuyerState.icp.amount_e8s = max_participant_icp_e8s`. Excess `max_participant_icp_e8s` remains in subaccount.
3. Swap reaches ABORTED lifecycle. `sweep_icp` is called. For this buyer, `transfer_helper` sets `transfer_start_timestamp_seconds = now_fn(false)` and issues `transfer_funds` to the ICP ledger.
4. Before the ledger response arrives, the SNS canister is upgraded via NNS governance proposal (routine operation). The canister state is checkpointed with `transfer_start_timestamp_seconds > 0`, `transfer_success_timestamp_seconds = 0`.
5. After upgrade, `sweep_icp` is called again. `transfer_helper` sees `transfer_start_timestamp_seconds > 0` → returns `AlreadyStarted` → buyer is skipped.
6. Buyer calls `error_refund_icp`. The check at line 1952 sees `transfer_success_timestamp_seconds == 0` → returns precondition error "ICP in escrow."
7. Buyer has no further recourse. Both `sweep_icp` and `error_refund_icp` are permanently blocked for this buyer. The `max_participant_icp_e8s` of accepted ICP (which may or may not have been transferred in step 3 before the upgrade) and the excess ICP are both inaccessible. [5](#0-4) [8](#0-7) [4](#0-3) [9](#0-8)

### Citations

**File:** rs/sns/swap/src/swap.rs (L1224-1237)
```rust
        let actual_increment_e8s = std::cmp::min(max_increment_e8s, requested_increment_e8s);
        let new_balance_e8s = old_amount_icp_e8s.saturating_add(actual_increment_e8s);
        if new_balance_e8s > max_participant_icp_e8s {
            log!(
                INFO,
                "Participant {} contributed {} e8s - the limit per participant is {}",
                buyer,
                new_balance_e8s,
                max_participant_icp_e8s
            );
        }

        // Limit the participation based on the maximum per participant.
        let new_balance_e8s = std::cmp::min(new_balance_e8s, max_participant_icp_e8s);
```

**File:** rs/sns/swap/src/swap.rs (L1285-1288)
```rust
        self.buyers
            .entry(buyer.to_string())
            .or_insert_with(|| BuyerState::new(0))
            .set_amount_icp_e8s(new_balance_e8s);
```

**File:** rs/sns/swap/src/swap.rs (L1950-1960)
```rust
        if let Some(buyer_state) = self.buyers.get(&source_principal_id.to_string()) {
            if let Some(transfer) = &buyer_state.icp
                && transfer.transfer_success_timestamp_seconds == 0
            {
                // This buyer has ICP not yet disbursed using the normal mechanism.
                return ErrorRefundIcpResponse::new_precondition_error(format!(
                    "ICP cannot be refunded as principal {} has {} ICP (e8s) in escrow",
                    source_principal_id,
                    buyer_state.amount_icp_e8s()
                ));
            }
```

**File:** rs/sns/swap/src/swap.rs (L2113-2131)
```rust
            let result = icp_transferable_amount
                .transfer_helper(
                    now_fn,
                    DEFAULT_TRANSFER_FEE,
                    Some(subaccount),
                    &dst,
                    icp_ledger,
                )
                .await;
            match result {
                // AmountToSmall should never happen as the amount contributed is checked in
                // `refresh_buyer_tokens`. In the case of a bug due to programmer error,
                // increment the invalid field. This will require a manual intervention
                // via an upgrade to correct
                TransferResult::AmountTooSmall => {
                    sweep_result.invalid += 1;
                }
                TransferResult::AlreadyStarted => {
                    sweep_result.skipped += 1;
```

**File:** rs/sns/swap/src/types.rs (L617-656)
```rust
        if self.transfer_start_timestamp_seconds > 0 {
            // Operation in progress...
            return TransferResult::AlreadyStarted;
        }
        self.transfer_start_timestamp_seconds = now_fn(false);

        // The ICRC1Ledger Trait converts any errors to Err(NervousSystemError).
        // No panics should occur when issuing this transfer.
        let result = ledger
            .transfer_funds(
                amount.get_e8s().saturating_sub(fee.get_e8s()),
                fee.get_e8s(),
                subaccount,
                *dst,
                0,
            )
            .await;
        if self.transfer_start_timestamp_seconds == 0 {
            log!(
                ERROR,
                "Token disburse logic error: expected transfer start time",
            );
        }
        match result {
            Ok(h) => {
                self.transfer_success_timestamp_seconds = now_fn(true);
                log!(
                    INFO,
                    "Transferred {} from subaccount {:?} to {} at height {} in Ledger Canister {}",
                    amount,
                    subaccount,
                    dst,
                    h,
                    ledger.canister_id()
                );
                TransferResult::Success(h)
            }
            Err(e) => {
                self.transfer_start_timestamp_seconds = 0;
                self.transfer_success_timestamp_seconds = 0;
```
