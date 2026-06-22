### Title
SNS Swap `refresh_buyer_token_e8s` Accepts Unintended ICP Transfers as Buyer Participation — (File: rs/sns/swap/src/swap.rs)

### Summary
The SNS Swap canister's `refresh_buyer_token_e8s` function verifies participation by checking the raw ICP ledger balance of a buyer's deterministic subaccount. Because any party can transfer ICP to that subaccount and any party can invoke `refresh_buyer_tokens` naming an arbitrary buyer, an unprivileged attacker can credit ICP participation to a victim's identity without the victim's knowledge or consent, and can manipulate whether a swap reaches its minimum-ICP commitment threshold.

### Finding Description

`refresh_buyer_token_e8s` in `rs/sns/swap/src/swap.rs` reads the current ICP balance of:

```
Account { owner: swap_canister_id, subaccount: Some(principal_to_subaccount(&buyer)) }
```

and computes the increment over the previously recorded `old_amount_icp_e8s`: [1](#0-0) 

The function then records `new_balance_e8s` as the buyer's accepted participation: [2](#0-1) 

The public canister endpoint accepts an arbitrary `buyer` field — it does not require the caller to match the named buyer: [3](#0-2) 

Because `principal_to_subaccount` is a deterministic, publicly computable function of the buyer's principal, any party can:

1. Compute `Account { owner: swap_canister_id, subaccount: Some(principal_to_subaccount(&victim)) }`.
2. Transfer ICP to that account from any ICP ledger account they control.
3. Call `refresh_buyer_tokens` with `buyer = victim`.

The function's own documentation acknowledges the assumption but does not enforce it: [4](#0-3) 

The ticket guard does not block this path. The ticket check only rejects calls where `amount_ticket > requested_increment_e8s`; if the attacker deposits ≥ ticket amount, the ticket is silently deleted and the full attacker-supplied balance is credited: [5](#0-4) 

### Impact Explanation

**Forced participation without consent.** An attacker sends ICP to a victim's swap subaccount and calls `refresh_buyer_tokens(buyer = victim)`. The victim's `BuyerState` is created or updated, their participation is locked in the swap, and — if the swap commits — the ICP is swept to the SNS governance treasury and the victim receives SNS tokens they never chose to acquire. The victim cannot undo this before the swap closes.

**Swap-outcome manipulation.** A swap that would otherwise abort (minimum ICP not reached) can be pushed past its `min_direct_participation_icp_e8s` threshold by an attacker who creates fresh principals, deposits ICP into their subaccounts, and calls `refresh_buyer_tokens` for each. This forces a `Committed` outcome and triggers SNS token distribution and ICP transfer to the SNS treasury, affecting all legitimate participants.

**Silent ticket deletion.** A buyer who created a ticket for amount X and deposited X ICP can have their ticket deleted by an attacker who deposits any additional amount Y and calls `refresh_buyer_tokens` for them, consuming the ticket and crediting X+Y participation — more than the buyer intended.

### Likelihood Explanation

The attack requires no privileged access. Any ICP ledger account holder can execute it. The subaccount derivation (`principal_to

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

**File:** rs/sns/swap/src/swap.rs (L1210-1225)
```rust
        let old_amount_icp_e8s = self
            .buyers
            .get(&buyer.to_string())
            .map_or(0, |buyer| buyer.amount_icp_e8s());

        if old_amount_icp_e8s >= e8s {
            // Already up-to-date. Strict inequality can happen if messages are re-ordered.
            return Ok(RefreshBuyerTokensResponse {
                icp_accepted_participation_e8s: old_amount_icp_e8s,
                icp_ledger_account_balance_e8s: e8s,
            });
        }
        // Subtraction safe because of the preceding if-statement.
        let requested_increment_e8s = e8s - old_amount_icp_e8s;
        let actual_increment_e8s = std::cmp::min(max_increment_e8s, requested_increment_e8s);
        let new_balance_e8s = old_amount_icp_e8s.saturating_add(actual_increment_e8s);
```

**File:** rs/sns/swap/src/swap.rs (L1250-1272)
```rust
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
