### Title
SNS Swap `refresh_buyer_token_e8s` Uses Entire Subaccount Balance Instead of Ticket-Specified Amount, Inflating Buyer Participation - (`File: rs/sns/swap/src/swap.rs`)

### Summary

The `refresh_buyer_token_e8s` function in the SNS Swap canister reads the **entire ICP balance** of a buyer's subaccount and uses it to compute the participation increment, even when the buyer has created a ticket specifying a smaller, exact participation amount. If residual ICP exists in the subaccount (from a prior failed transaction, a previous partial participation, or any other source), the buyer is silently enrolled for a larger participation than they intended, with no way to correct this until the swap closes.

### Finding Description

In `rs/sns/swap/src/swap.rs`, `refresh_buyer_token_e8s` queries the full balance of the buyer's per-principal subaccount on the ICP ledger:

```rust
let e8s = {
    let account = Account {
        owner: this_canister.get().0,
        subaccount: Some(principal_to_subaccount(&buyer)),
    };
    icp_ledger
        .account_balance(account)
        .await
        .map_err(|x| x.to_string())?
        .get_e8s()
};
``` [1](#0-0) 

The increment applied to the buyer's participation is then:

```rust
let requested_increment_e8s = e8s - old_amount_icp_e8s;
let actual_increment_e8s = std::cmp::min(max_increment_e8s, requested_increment_e8s);
``` [2](#0-1) 

The ticket validation only enforces a **lower bound** on the increment — it rejects if the subaccount balance is *less* than the ticket amount, but silently accepts (and uses) any *excess*:

```rust
if amount_ticket > requested_increment_e8s {
    return Err(format!(
        "The available balance to be topped up ({requested_increment_e8s}) \
        by the buyer is smaller than the amount requested ({amount_ticket})."
    ));
}
memory::OPEN_TICKETS_MEMORY.with(|m| m.borrow_mut().remove(&principal));
``` [3](#0-2) 

The ticket is the user's explicit statement of intent ("I want to participate with exactly X ICP"). The check only enforces `balance >= ticket_amount`, not `balance == ticket_amount`. Any ICP above the ticket amount — whether from a prior failed transfer, a previous partial participation, or an accidental double-send — is silently swept into the participation.

### Impact Explanation

A buyer who:
1. Has 10 ICP of residual ICP in their swap subaccount (from a prior failed transaction),
2. Creates a ticket for 100 ICP,
3. Transfers 100 ICP to the subaccount (now 110 ICP total), and
4. Calls `refresh_buyer_tokens`,

will be enrolled for **110 ICP** of participation instead of the intended 100 ICP. The extra 10 ICP is locked in the swap canister until the swap closes (committed or aborted). During an open swap, there is no mechanism to reduce participation or reclaim the excess. The `error_refund_icp` path is only available post-close:

```rust
if !(self.lifecycle() == Lifecycle::Aborted || self.lifecycle() == Lifecycle::Committed) {
    return ErrorRefundIcpResponse::new_precondition_error(
        "Error refunds can only be performed when the swap is ABORTED or COMMITTED",
    );
}
``` [4](#0-3) 

The participation is bounded by `max_participant_icp_e8s`, but within that cap the buyer's ICP commitment is inflated beyond their stated intent. If the swap commits, the buyer receives more SNS tokens than they wanted (and paid more ICP than intended). If the swap aborts, the ICP is eventually returned, but was locked for the entire swap duration.

### Likelihood Explanation

This is reachable by any unprivileged ingress caller. Residual ICP in a buyer's subaccount is a realistic condition:
- A prior `refresh_buyer_tokens` call that failed after the ledger transfer but before the swap state update leaves ICP stranded.
- A user who participated in an earlier phase and had ICP partially refunded.
- A user who accidentally sent a double transfer.

The SNS Swap canister itself documents this scenario in the `error_refund_icp` function, acknowledging that subaccounts can hold ICP that was not properly accounted for:

```rust
// This buyer is not known to the swap canister. Any
// balance in a subaccount belongs to the buyer.
``` [5](#0-4) 

### Recommendation

After the ticket check passes, cap the actual increment to the ticket amount rather than the full subaccount balance increment:

```rust
let actual_increment_e8s = if let Some(ticket) = ticket_sns_sale_canister {
    std::cmp::min(ticket.amount_icp_e8s, requested_increment_e8s)
} else {
    requested_increment_e8s
};
let actual_increment_e8s = std::cmp::min(max_increment_e8s, actual_increment_e8s);
```

Any ICP above the ticket amount that remains in the subaccount should be refundable immediately (not only post-close), or the ticket check should be a strict equality guard.

### Proof of Concept

1. SNS swap is open with `min_participant_icp_e8s = 1 ICP`, `max_participant_icp_e8s = 200 ICP`.
2. Alice previously had a failed `refresh_buyer_tokens` call that left 10 ICP stranded in her subaccount (`old_amount_icp_e8s = 0`, subaccount balance = 10 ICP).
3. Alice creates a ticket for 100 ICP via `new_sale_ticket`.
4. Alice transfers 100 ICP to her subaccount (subaccount balance = 110 ICP).
5. Alice calls `refresh_buyer_tokens`.
6. `e8s = 110`, `old_amount_icp_e8s = 0`, `requested_increment_e8s = 110`.
7. Ticket check: `amount_ticket (100) > requested_increment_e8s (110)`? No → passes.
8. `actual_increment_e8s = min(available_direct_participation, 110) = 110`.
9. Alice is enrolled for 110 ICP participation instead of her intended 100 ICP.
10. The extra 10 ICP is locked until the swap closes; Alice cannot reclaim it during the open swap. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/sns/swap/src/swap.rs (L1134-1163)
```rust
    pub async fn refresh_buyer_token_e8s(
        &mut self,
        buyer: PrincipalId,
        confirmation_text: Option<String>,
        this_canister: CanisterId,
        icp_ledger: &dyn ICRC1Ledger,
    ) -> Result<RefreshBuyerTokensResponse, String> {
        use swap_participation::*;

        // These two checks need to be repeated after awaiting the response from the ICP ledger.
        self.validate_lifecycle_is_open()
            .map_err(context_before_awaiting_icp_ledger_response)?;
        self.validate_possibility_of_direct_participation()
            .map_err(context_before_awaiting_icp_ledger_response)?;

        // User input validation doesn't expire after await, so this check doesn't need repetition.
        self.validate_confirmation_text(confirmation_text)?;

        // Look for the token balance of the specified principal's subaccount on 'this' canister.
        let e8s = {
            let account = Account {
                owner: this_canister.get().0,
                subaccount: Some(principal_to_subaccount(&buyer)),
            };
            icp_ledger
                .account_balance(account)
                .await
                .map_err(|x| x.to_string())?
                .get_e8s()
        };
```

**File:** rs/sns/swap/src/swap.rs (L1222-1225)
```rust
        // Subtraction safe because of the preceding if-statement.
        let requested_increment_e8s = e8s - old_amount_icp_e8s;
        let actual_increment_e8s = std::cmp::min(max_increment_e8s, requested_increment_e8s);
        let new_balance_e8s = old_amount_icp_e8s.saturating_add(actual_increment_e8s);
```

**File:** rs/sns/swap/src/swap.rs (L1248-1272)
```rust
        // Try to fetch the current ticket of the buyer
        let principal = Blob::from_bytes(buyer.as_slice().into());
        if let Some(ticket_sns_sale_canister) =
            memory::OPEN_TICKETS_MEMORY.with(|m| m.borrow().get(&principal))
        {
            let amount_ticket = ticket_sns_sale_canister.amount_icp_e8s;
            // If the user has already bought tokens in this swap at a prior to the current purchase the
            // balance in the subaccount of the SNS sales canister that corresponds to the user will
            // show both the ICP balance used for the previous buy and the ICP balance used to make
            // this new purchase of SNS tokens (requested_increment_e8s + old_amount_icp_e8s).
            // If the ticket has a lower amount specified than what is the requested amount of
            // tokens according to the ICP balance in the subaccount, this check should pass
            // and the actual requested amount of tokens will be used.
            // Lower amounts than specified on the ticket are not excepted.
            if amount_ticket > requested_increment_e8s {
                return Err(format!(
                    "The available balance to be topped up ({requested_increment_e8s}) \
                    by the buyer is smaller than the amount requested ({amount_ticket})."
                ));
            }
            // The requested balance in the ticket matches the balance to be topped up in the swap
            // --> Delete fully executed ticket, if it exists and proceed with the top up
            memory::OPEN_TICKETS_MEMORY.with(|m| m.borrow_mut().remove(&principal));
            // If there exists no ticket for the buyer, the payment flow will simply ignore the ticket
        }
```

**File:** rs/sns/swap/src/swap.rs (L1932-1936)
```rust
        if !(self.lifecycle() == Lifecycle::Aborted || self.lifecycle() == Lifecycle::Committed) {
            return ErrorRefundIcpResponse::new_precondition_error(
                "Error refunds can only be performed when the swap is ABORTED or COMMITTED",
            );
        }
```

**File:** rs/sns/swap/src/swap.rs (L1966-1969)
```rust
        } else {
            // This buyer is not known to the swap canister. Any
            // balance in a subaccount belongs to the buyer.
        }
```
