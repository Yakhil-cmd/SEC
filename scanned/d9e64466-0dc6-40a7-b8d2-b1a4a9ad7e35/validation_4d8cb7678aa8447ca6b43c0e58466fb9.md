### Title
ICP Sent to SNS Swap Subaccount Is Locked While Swap Remains OPEN When `refresh_buyer_tokens` Fails Due to Full Participation - (File: rs/sns/swap/src/swap.rs)

### Summary

The SNS Swap canister uses a two-step participation flow: (1) a buyer transfers ICP to their principal-derived subaccount of the Swap canister on the ICP ledger, then (2) calls `refresh_buyer_tokens` to register the participation. If step 2 fails because the swap's direct participation cap is already reached, the ICP sits in the buyer's subaccount untracked in `self.buyers`. The only recovery path, `error_refund_icp`, is gated behind the swap being in `Aborted` or `Committed` state. While the swap remains `Open`, the buyer's ICP is inaccessible.

### Finding Description

`refresh_buyer_token_e8s` in `rs/sns/swap/src/swap.rs` performs two lifecycle/capacity checks: one before the async ICP ledger balance query and one after. [1](#0-0) [2](#0-1) 

If `validate_possibility_of_direct_participation` fails at either point (because `available_direct_participation_e8s()` is zero), the function returns an `Err` string. [3](#0-2) 

At that point the buyer's ICP is already on the ICP ledger in the swap canister's subaccount for that principal, but `self.buyers` is never updated. The buyer is not registered and has no `BuyerState`.

The only way to recover unregistered ICP is `error_refund_icp`, which unconditionally rejects calls while the swap is `Open`: [4](#0-3) 

The code itself acknowledges this gap with an open TODO: [5](#0-4) 

The same gap exists for the per-participant cap: when a buyer sends more ICP than `max_participant_icp_e8s`, only the capped amount is recorded in `self.buyers`; the excess remains in the subaccount and is also unreachable via `error_refund_icp` while the swap is `Open`. [6](#0-5) 

### Impact Explanation

A buyer who transfers ICP to the Swap canister's subaccount and whose `refresh_buyer_tokens` call is rejected (swap full, per-participant cap exceeded, or any other post-transfer error) has their ICP locked for the entire remaining open period of the swap, which can be up to 90 days per the `swap_due_timestamp_seconds` parameter. Unlike the Solidity analog where funds could be permanently locked, here the ICP is recoverable once the swap reaches `Committed` or `Aborted` state. However, the buyer loses liquidity for an extended, unpredictable period with no on-chain mechanism to reclaim funds early. If the swap commits successfully, the buyer's unregistered ICP is swept back via `error_refund_icp` post-commit, but the buyer receives no SNS tokens for it. [7](#0-6) 

### Likelihood Explanation

This is reachable by any unprivileged user. The two-step flow (ledger transfer then `refresh_buyer_tokens`) is the documented participation path. A race condition where the swap fills between the transfer and the notify call is realistic in a popular SNS launch. A user who intentionally or accidentally sends more than `max_participant_icp_e8s` also hits the excess-locking path. No privileged access is required. [8](#0-7) 

### Recommendation

Implement the acknowledged TODO (NNS1-1682): when `refresh_buyer_token_e8s` determines that none or only part of the transferred ICP can be accepted, the canister should immediately attempt an async refund of the unacceptable portion back to the buyer's principal account on the ICP ledger, rather than leaving it stranded in the subaccount. Alternatively, expose a `refund_unaccepted_icp` endpoint callable while the swap is `Open` that checks whether the caller's subaccount balance exceeds their registered `BuyerState.icp.amount_e8s` and refunds the difference.

### Proof of Concept

1. SNS swap is `Open`; `available_direct_participation_e8s()` returns 0 (cap reached).
2. Buyer transfers 5 ICP to `Account { owner: swap_canister_id, subaccount: principal_to_subaccount(buyer) }` on the ICP ledger.
3. Buyer calls `refresh_buyer_tokens`. `validate_possibility_of_direct_participation` fails; function returns `Err(...)`. `self.buyers` is not updated.
4. Buyer calls `error_refund_icp`. Returns `ErrorRefundIcpResponse::new_precondition_error("Error refunds can only be performed when the swap is ABORTED or COMMITTED")`.
5. Buyer's 5 ICP remains locked in the subaccount for the remainder of the swap's open period (potentially weeks). [9](#0-8) [10](#0-9)

### Citations

**File:** rs/sns/swap/src/swap.rs (L1128-1133)
```rust
    /// If a ledger transfer was successfully made, but this call
    /// fails (many reasons are possible), the owner of the ICP sent
    /// to the subaccount can reclaim their tokens using `error_refund_icp`
    /// once this swap is closed (committed or aborted).
    ///
    /// TODO(NNS1-1682): attempt to refund ICP that cannot be accepted.
```

**File:** rs/sns/swap/src/swap.rs (L1144-1147)
```rust
        self.validate_lifecycle_is_open()
            .map_err(context_before_awaiting_icp_ledger_response)?;
        self.validate_possibility_of_direct_participation()
            .map_err(context_before_awaiting_icp_ledger_response)?;
```

**File:** rs/sns/swap/src/swap.rs (L1168-1171)
```rust
        self.validate_lifecycle_is_open()
            .map_err(context_after_awaiting_icp_ledger_response)?;
        self.validate_possibility_of_direct_participation()
            .map_err(context_after_awaiting_icp_ledger_response)?;
```

**File:** rs/sns/swap/src/swap.rs (L1177-1177)
```rust
        let max_increment_e8s = self.available_direct_participation_e8s();
```

**File:** rs/sns/swap/src/swap.rs (L1236-1237)
```rust
        // Limit the participation based on the maximum per participant.
        let new_balance_e8s = std::cmp::min(new_balance_e8s, max_participant_icp_e8s);
```

**File:** rs/sns/swap/src/swap.rs (L1285-1291)
```rust
        self.buyers
            .entry(buyer.to_string())
            .or_insert_with(|| BuyerState::new(0))
            .set_amount_icp_e8s(new_balance_e8s);
        // We compute the current participation amounts once and store the result in Swap's state,
        // for efficiency reasons.
        self.update_total_participation_amounts();
```

**File:** rs/sns/swap/src/swap.rs (L1925-1936)
```rust
    pub async fn error_refund_icp(
        &self,
        self_canister_id: CanisterId,
        request: &ErrorRefundIcpRequest,
        icp_ledger: &dyn ICRC1Ledger,
    ) -> ErrorRefundIcpResponse {
        // Fail if the request is premature.
        if !(self.lifecycle() == Lifecycle::Aborted || self.lifecycle() == Lifecycle::Committed) {
            return ErrorRefundIcpResponse::new_precondition_error(
                "Error refunds can only be performed when the swap is ABORTED or COMMITTED",
            );
        }
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

**File:** rs/sns/swap/canister/canister.rs (L127-142)
```rust
#[update]
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    log!(INFO, "refresh_buyer_tokens");
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()
    };
    let icp_ledger = create_real_icp_ledger(swap().init_or_panic().icp_ledger_or_panic());
    match swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, this_canister_id(), &icp_ledger)
        .await
    {
        Ok(r) => r,
        Err(msg) => panic!("{}", msg),
    }
```
