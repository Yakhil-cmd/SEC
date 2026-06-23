### Title
ICP Sent to SNS Swap Canister Subaccount Is Temporarily Locked with No Refund Path During OPEN State — (`rs/sns/swap/src/swap.rs`)

---

### Summary

The SNS Swap canister (`rs/sns/swap/src/swap.rs`) requires buyers to first transfer ICP to a per-principal subaccount of the swap canister on the ICP ledger, then call `refresh_buyer_tokens` to register their participation. However, ICP that is sent to the subaccount but cannot be accepted — either because it falls below `min_participant_icp_e8s`, exceeds `max_participant_icp_e8s`, or exceeds the remaining swap capacity — is not tracked in the swap's internal state and has no refund path while the swap is in the `OPEN` lifecycle state. The only recovery mechanism, `error_refund_icp`, is explicitly blocked during `OPEN`. This is a direct analog to the external report's pattern: tokens sent to a contract that bypass the accounting mechanism become inaccessible until a future state transition.

---

### Finding Description

**Step 1 — ICP enters the subaccount but is not fully accounted for.**

In `refresh_buyer_token_e8s`, the swap reads the ICP balance of the buyer's subaccount and caps the accepted participation at `max_participant_icp_e8s` and at the remaining swap capacity (`max_increment_e8s`): [1](#0-0) 

If a buyer sends 10 ICP but `max_participant_icp_e8s` is 5 ICP, the swap records 5 ICP in `self.buyers` and the remaining 5 ICP sits in the subaccount, completely untracked by the swap's state. Similarly, if the balance is below `min_participant_icp_e8s`, `refresh_buyer_tokens` returns an error and the buyer is never inserted into `self.buyers` at all: [2](#0-1) 

The code itself acknowledges this gap with an open TODO: [3](#0-2) 

**Step 2 — The only refund mechanism is blocked during OPEN.**

`error_refund_icp` is the sole mechanism for recovering ICP from a buyer's subaccount. It unconditionally rejects all calls when the lifecycle is not `ABORTED` or `COMMITTED`: [4](#0-3) 

There is no alternative path to recover ICP during the `OPEN` state.

**Step 3 — Even after the swap closes, a second gate blocks refund until sweep completes.**

After the swap closes but before `sweep_icp` runs, any buyer who is present in `self.buyers` with `transfer_success_timestamp_seconds == 0` is also blocked from calling `error_refund_icp`: [5](#0-4) 

This means a buyer who sent excess ICP (above `max_participant_icp_e8s`) must wait for both the swap to close AND `sweep_icp` to successfully complete before the excess can be recovered. `sweep_icp` only transfers the recorded `amount_e8s` (the capped amount), leaving the excess in the subaccount, but the buyer cannot call `error_refund_icp` until `transfer_success_timestamp_seconds` is set.

**Concrete attack/user path:**

1. SNS swap is in `OPEN` state with `max_participant_icp_e8s = 5 ICP`.
2. User transfers 10 ICP to their subaccount (`swap_canister_id` / `principal_to_subaccount(user)`) on the ICP ledger.
3. User calls `refresh_buyer_tokens`. The swap records 5 ICP; the other 5 ICP is untracked.
4. User calls `error_refund_icp` to recover the 5 ICP excess → rejected: "Error refunds can only be performed when the swap is ABORTED or COMMITTED".
5. The 5 ICP excess is locked for the entire duration of the `OPEN` state (which can span days to weeks per swap parameters).

The same scenario applies to ICP sent below `min_participant_icp_e8s`: the ICP is in the subaccount, `refresh_buyer_tokens` fails, the buyer is not in `self.buyers`, and `error_refund_icp` is still blocked during `OPEN`.

---

### Impact Explanation

Any ICP sent to the SNS Swap canister's buyer subaccount that is not fully accepted by `refresh_buyer_tokens` — due to per-participant caps, swap-level caps, or minimum thresholds — is inaccessible for the entire duration of the `OPEN` lifecycle. SNS swaps can remain open for extended periods (days to weeks). The locked amount is bounded only by what the user sent, which could be substantial. While the ICP is eventually recoverable after the swap closes and sweep completes, the temporary lock constitutes a real ledger conservation impact: ICP is held by the swap canister with no mechanism for the rightful owner to retrieve it during the active period. [6](#0-5) 

---

### Likelihood Explanation

This is reachable by any unprivileged ICP ledger user. Realistic triggers include:

- A user accidentally sends more ICP than `max_participant_icp_e8s`.
- A user sends ICP and the swap reaches its `max_direct_participation_icp_e8s` cap between the transfer and the `refresh_buyer_tokens` call, causing only a partial amount to be accepted.
- A user sends ICP below `min_participant_icp_e8s` (e.g., due to a fee deduction reducing the amount below the threshold).
- A user participates in multiple SNS swaps and confuses the amounts.

The two-step deposit flow (transfer then notify) is inherently racy and error-prone, making these scenarios practically likely.

---

### Recommendation

1. **Immediate refund of unaccepted ICP**: In `refresh_buyer_token_e8s`, after capping the accepted amount, immediately transfer any excess ICP back to the buyer's principal. The TODO at line 1133 (`TODO(NNS1-1682): attempt to refund ICP that cannot be accepted`) already identifies this as the correct fix.
2. **Allow `error_refund_icp` during OPEN for unregistered buyers**: If a principal is not present in `self.buyers`, allow `error_refund_icp` to run during `OPEN` as well, since there is no accounting conflict for unknown principals.
3. **Documentation**: Add explicit warnings to the `refresh_buyer_tokens` interface documentation that excess ICP cannot be recovered during the `OPEN` state.

---

### Proof of Concept

```
// Preconditions:
// - SNS swap is OPEN
// - max_participant_icp_e8s = 5 ICP
// - min_participant_icp_e8s = 1 ICP

// Step 1: Transfer 10 ICP to the swap canister's subaccount for the user
icp_ledger.transfer({
    to: swap_canister_subaccount(user_principal),
    amount: 10 ICP
});

// Step 2: Notify the swap — only 5 ICP is accepted, 5 ICP is untracked
swap.refresh_buyer_tokens({ buyer: user_principal });
// => Ok { icp_accepted_participation_e8s: 5 ICP, icp_ledger_account_balance_e8s: 10 ICP }

// Step 3: Attempt to recover the 5 ICP excess
swap.error_refund_icp({ source_principal_id: user_principal });
// => Err { error_type: Precondition,
//          description: "Error refunds can only be performed when the swap is ABORTED or COMMITTED" }

// Result: 5 ICP is locked in the subaccount for the entire OPEN duration.
// The swap canister holds ICP it has no accounting record for,
// and the rightful owner has no recourse until the swap closes.
``` [7](#0-6) [8](#0-7) [4](#0-3) [9](#0-8)

### Citations

**File:** rs/sns/swap/src/swap.rs (L1113-1134)
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
    pub async fn refresh_buyer_token_e8s(
```

**File:** rs/sns/swap/src/swap.rs (L1200-1207)
```rust
        // Check that the minimum amount has been transferred before
        // actually creating an entry for the buyer.
        if e8s < params.min_participant_icp_e8s {
            return Err(format!(
                "Amount transferred: {}; minimum required to participate: {}",
                e8s, params.min_participant_icp_e8s
            ));
        }
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

**File:** rs/sns/swap/canister/canister.rs (L127-143)
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
}
```
