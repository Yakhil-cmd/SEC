### Title
Excess ICP Sent to SNS Swap Subaccount Is Locked Without Automatic Refund When Participation Caps Are Exceeded - (`rs/sns/swap/src/swap.rs`)

---

### Summary

In the SNS Swap canister's `refresh_buyer_token_e8s` function, when a buyer transfers more ICP to their swap subaccount than the per-participant cap (`max_participant_icp_e8s`) or the global remaining cap (`available_direct_participation_e8s`), the excess ICP is silently accepted by the subaccount but only the capped amount is credited as participation. The excess ICP is locked in the subaccount for the entire duration of the swap (potentially weeks) and is only recoverable via `error_refund_icp` after the swap closes **and** `sweep_icp` completes. There is no automatic refund of excess ICP. The developers themselves acknowledge this with a TODO comment at the exact site of the root cause.

---

### Finding Description

`refresh_buyer_token_e8s` reads the actual ICP balance from the ledger, then caps the accepted participation at `min(max_increment_e8s, requested_increment_e8s)` and further at `max_participant_icp_e8s`: [1](#0-0) 

The function records only `new_balance_e8s` (the capped amount) in `self.buyers`, and returns both values to the caller: [2](#0-1) 

The excess ICP (`icp_ledger_account_balance_e8s - icp_accepted_participation_e8s`) remains stranded in the buyer's subaccount. The developers explicitly acknowledge this is unresolved: [3](#0-2) 

The only recovery path is `error_refund_icp`, which is blocked during the `Open` lifecycle: [4](#0-3) 

And further blocked even after the swap closes, until `sweep_icp` has completed for that buyer (`transfer_success_timestamp_seconds` must be non-zero): [5](#0-4) 

Only after both conditions are met can the user manually call `error_refund_icp` to recover the excess: [6](#0-5) 

---

### Impact Explanation

A buyer who sends more ICP than the applicable cap:
- Does **not** receive SNS tokens for the excess ICP (it is not credited as participation).
- Has the excess ICP locked in their subaccount for the entire swap duration (SNS swaps can run for weeks).
- Must manually invoke `error_refund_icp` after the swap closes **and** after `sweep_icp` has processed their entry — a multi-step, non-obvious recovery path.
- If the user is unaware of `error_refund_icp`, the excess ICP appears permanently lost from their perspective.

This is a direct ledger conservation bug: the protocol accepts more ICP than it credits, and the user receives no SNS tokens corresponding to the excess, mirroring the M-09 pattern exactly.

---

### Likelihood Explanation

This is reachable by any unprivileged ingress sender participating in an SNS swap. Scenarios include:
- A user who sends 10 ICP when `max_participant_icp_e8s` is 5 ICP (common for swaps with tight per-participant limits).
- A user who sends ICP when the global `available_direct_participation_e8s` is nearly exhausted, causing partial acceptance.
- A user who calls `refresh_buyer_token_e8s` multiple times and the second call finds the subaccount balance exceeds the remaining cap.

The `RefreshBuyerTokensResponse` does expose both values, but non-technical users relying on wallets or frontends that only display the accepted amount will not notice the discrepancy.

---

### Recommendation

Implement the acknowledged TODO at line 1133. After computing `new_balance_e8s`, if `e8s > new_balance_e8s`, immediately initiate an ICP ledger transfer returning the excess (`e8s - new_balance_e8s`, minus the transfer fee) back to the buyer's principal account before returning from `refresh_buyer_token_e8s`. This eliminates the lock-up period and removes the need for users to discover and invoke `error_refund_icp` manually.

---

### Proof of Concept

1. A swap is open with `max_participant_icp_e8s = 5 ICP`.
2. A buyer transfers 10 ICP to their subaccount on the swap canister.
3. Buyer calls `refresh_buyer_token_e8s`.
4. The function reads `e8s = 10 ICP`, computes `new_balance_e8s = 5 ICP`, stores 5 ICP as participation.
5. Response: `icp_accepted_participation_e8s = 5 ICP`, `icp_ledger_account_balance_e8s = 10 ICP`.
6. The 5 ICP excess is locked in the subaccount. `error_refund_icp` returns `"Error refunds can only be performed when the swap is ABORTED or COMMITTED"` if called now.
7. The swap runs for weeks. The buyer receives SNS tokens only for 5 ICP.
8. After the swap commits and `sweep_icp` runs, the buyer can finally call `error_refund_icp` to recover the 5 ICP excess — minus an additional transfer fee. [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

**File:** rs/sns/swap/src/swap.rs (L1113-1133)
```rust
    /// In state Open, this method can be called to refresh the amount
    /// of ICP a buyer has contributed from the ICP ledger canister.
    ///
    /// It is assumed that prior to calling this method, tokens have
    /// been transferred by the buyer to a subaccount of the swap
    /// canister (this canister) on the ICP ledger.
    /// Also, deletes an existing ticket if it has been fully executed
    /// (i.e. the requested increment is >= that the ticket amount).
    /// (This allows participation to be increased later.)
    ///
    /// If the SNS had specified a swap confirmation text, the caller of this
    /// function must accept this confirmation by sending the exact same text
    /// as an argument to this function (otherwise, the call will result in
    /// an error).
    ///
    /// If a ledger transfer was successfully made, but this call
    /// fails (many reasons are possible), the owner of the ICP sent
    /// to the subaccount can reclaim their tokens using `error_refund_icp`
    /// once this swap is closed (committed or aborted).
    ///
    /// TODO(NNS1-1682): attempt to refund ICP that cannot be accepted.
```

**File:** rs/sns/swap/src/swap.rs (L1177-1177)
```rust
        let max_increment_e8s = self.available_direct_participation_e8s();
```

**File:** rs/sns/swap/src/swap.rs (L1222-1237)
```rust
        // Subtraction safe because of the preceding if-statement.
        let requested_increment_e8s = e8s - old_amount_icp_e8s;
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

**File:** rs/sns/swap/src/swap.rs (L1308-1311)
```rust
        Ok(RefreshBuyerTokensResponse {
            icp_accepted_participation_e8s: new_balance_e8s,
            icp_ledger_account_balance_e8s: e8s,
        })
```

**File:** rs/sns/swap/src/swap.rs (L1932-1935)
```rust
        if !(self.lifecycle() == Lifecycle::Aborted || self.lifecycle() == Lifecycle::Committed) {
            return ErrorRefundIcpResponse::new_precondition_error(
                "Error refunds can only be performed when the swap is ABORTED or COMMITTED",
            );
```

**File:** rs/sns/swap/src/swap.rs (L1950-1959)
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
```

**File:** rs/sns/swap/src/swap.rs (L1990-2004)
```rust
        // Make transfer.
        let amount_e8s = balance_e8s.saturating_sub(DEFAULT_TRANSFER_FEE.get_e8s());
        let dst = Account {
            owner: source_principal_id.0,
            subaccount: None,
        };
        let transfer_result = icp_ledger
            .transfer_funds(
                amount_e8s,
                DEFAULT_TRANSFER_FEE.get_e8s(),
                Some(source_subaccount),
                dst,
                0, // memo
            )
            .await;
```
