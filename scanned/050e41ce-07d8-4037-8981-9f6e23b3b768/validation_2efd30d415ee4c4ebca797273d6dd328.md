Now I have enough information to write the finding. Let me verify the exact line numbers for the key code.### Title
Unauthenticated `buyer` Parameter in `refresh_buyer_tokens` Allows Anyone to Lock Victim's ICP in SNS Swap Escrow - (File: rs/sns/swap/canister/canister.rs)

### Summary

The `refresh_buyer_tokens` update endpoint on the SNS Swap canister accepts an arbitrary `buyer` principal in its request payload without verifying it matches the actual caller. Any unprivileged ingress sender can supply a victim's principal as the `buyer` field, causing the victim's ICP (already transferred to the swap subaccount) to be committed into the swap's escrow and their open ticket to be deleted — all without the victim's consent.

### Finding Description

In `rs/sns/swap/canister/canister.rs`, the `refresh_buyer_tokens` handler reads the effective buyer principal from the request argument rather than from the authenticated caller:

```rust
#[update]
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()          // safe path
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()  // ← no caller check
    };
    ...
    swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, this_canister_id(), &icp_ledger)
        .await
``` [1](#0-0) 

When `arg.buyer` is non-empty, the function uses the attacker-supplied principal `p` for all downstream operations. The inner `refresh_buyer_token_e8s` function then:

1. Reads the ICP balance of `principal_to_subaccount(&buyer)` on the swap canister — i.e., the victim's subaccount.
2. Validates the `confirmation_text` (a publicly visible string set at SNS init time).
3. Inserts the victim into the `buyers` map with their committed ICP amount.
4. **Deletes the victim's open ticket** from `OPEN_TICKETS_MEMORY`. [2](#0-1) [3](#0-2) [4](#0-3) 

Once a buyer is present in the `buyers` map with `transfer_success_timestamp_seconds == 0`, `error_refund_icp` explicitly blocks any refund:

```rust
if let Some(buyer_state) = self.buyers.get(&source_principal_id.to_string()) {
    if let Some(transfer) = &buyer_state.icp
        && transfer.transfer_success_timestamp_seconds == 0
    {
        return ErrorRefundIcpResponse::new_precondition_error(format!(
            "ICP cannot be refunded as principal {} has {} ICP (e8s) in escrow",
            ...
        ));
    }
}
``` [5](#0-4) 

Furthermore, `error_refund_icp` itself is only callable after the swap reaches `Lifecycle::Aborted` or `Lifecycle::Committed`: [6](#0-5) 

The `RefreshBuyerTokensRequest.buyer` field is documented as "If not specified, the caller is used," implying the non-empty path is intended for third-party helpers — but no authorization check is enforced. [7](#0-6) 

### Impact Explanation

An attacker who calls `refresh_buyer_tokens` with a victim's principal as `buyer`:

- **Locks the victim's ICP in escrow** for the entire remaining duration of the swap (up to 90 days). The victim cannot reclaim their ICP via `error_refund_icp` while the swap is open.
- **Destroys the victim's open ticket**, breaking the ticket-based payment flow the victim was using. The victim cannot create a new ticket while one exists, and after the attacker's call deletes it, the victim loses the amount and subaccount they had committed to.
- **Bypasses the confirmation text requirement**: if the SNS configured a mandatory `confirmation_text`, the attacker can supply it (it is a public value) and register the victim's participation without the victim ever having agreed to the swap terms.
- If the swap commits, the victim's ICP is swept to the SNS governance treasury and the victim receives SNS tokens they never consented to purchase.

### Likelihood Explanation

The attack requires no special privileges, no governance majority, and no cryptographic material. Any IC user can submit an ingress update call to the swap canister's `refresh_buyer_tokens` endpoint with an arbitrary `buyer` string. The victim only needs to have transferred ICP to their swap subaccount (the normal first step of participation). The `confirmation_text` is readable from `get_init` (a public query). The attack is therefore trivially executable by any observer of on-chain state.

### Recommendation

Remove the unauthenticated `buyer` override. The effective buyer should always be derived from the authenticated caller:

```rust
#[update]
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let p = caller_principal_id();   // always use the authenticated caller
    ...
    swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, this_canister_id(), &icp_ledger)
        .await
}
```

If third-party notification of another user's participation is a desired feature (e.g., a helper canister calling on behalf of a user), it should be gated by an explicit allowlist or require the victim's principal to match the caller, analogous to how `new_sale_ticket` always uses `caller_principal_id()` with no override. [8](#0-7) 

### Proof of Concept

1. **Setup**: SNS swap is in `Lifecycle::Open`. Alice calls `new_sale_ticket` (which correctly uses `caller_principal_id()`) to create a ticket for `amount = min_participant_icp_e8s`. Alice then transfers that ICP to `Account { owner: swap_canister_id, subaccount: principal_to_subaccount(alice) }` on the ICP ledger.

2. **Attack**: Before Alice calls `refresh_buyer_tokens`, attacker Bob submits:
   ```
   refresh_buyer_tokens({
       buyer: alice_principal.to_text(),
       confirmation_text: Some("<public confirmation text from get_init>"),
   })
   ```
   Bob is the ingress sender; Alice's principal is in the payload.

3. **Effect**: The swap canister reads Alice's subaccount balance (≥ `min_participant_icp_e8s`), validates the public confirmation text, inserts Alice into `self.buyers` with her ICP amount, and deletes Alice's open ticket from `OPEN_TICKETS_MEMORY`.

4. **Consequence**: Alice's ICP is now in escrow. If she calls `error_refund_icp` while the swap is open, it is rejected with "Error refunds can only be performed when the swap is ABORTED or COMMITTED." If she tries to create a new ticket, it succeeds (the old one was deleted), but her ICP is already committed at the old amount. If the swap commits, Alice's ICP is sent to the SNS treasury and she receives SNS tokens she did not explicitly agree to purchase.

### Citations

**File:** rs/sns/swap/canister/canister.rs (L128-143)
```rust
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

**File:** rs/sns/swap/canister/canister.rs (L231-235)
```rust
#[update]
async fn new_sale_ticket(request: NewSaleTicketRequest) -> NewSaleTicketResponse {
    log!(INFO, "new_sale_ticket");
    swap_mut().new_sale_ticket(&request, caller_principal_id(), time())
}
```

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

**File:** rs/sns/swap/src/swap.rs (L1931-1936)
```rust
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

**File:** rs/sns/swap/src/gen/ic_sns_swap.pb.v1.rs (L1117-1126)
```rust
pub struct RefreshBuyerTokensRequest {
    /// If not specified, the caller is used.
    #[prost(string, tag = "1")]
    pub buyer: ::prost::alloc::string::String,
    /// To accept the swap participation confirmation, a participant should send
    /// the confirmation text via refresh_buyer_tokens, matching the text set
    /// during SNS initialization.
    #[prost(string, optional, tag = "2")]
    pub confirmation_text: ::core::option::Option<::prost::alloc::string::String>,
}
```
