Audit Report

## Title
Unauthenticated `refresh_buyer_tokens` Allows Any Caller to Bypass `confirmation_text` Consent on Behalf of Any Buyer — (File: `rs/sns/swap/canister/canister.rs`)

## Summary

The SNS Swap canister's `refresh_buyer_tokens` update endpoint accepts an arbitrary `buyer` principal in its request body without verifying that the caller equals that buyer. When an SNS is configured with a `confirmation_text` consent gate, any unprivileged ingress sender can supply a victim's principal and the publicly readable `confirmation_text`, committing the victim's ICP to the swap without the victim ever explicitly agreeing to the terms. The `confirmation_text` feature's entire purpose is to enforce explicit regulatory or legal consent; this path renders it a no-op.

## Finding Description

**Root cause — no caller == buyer check:**

In `rs/sns/swap/canister/canister.rs` lines 130–134, when `arg.buyer` is non-empty the resolved principal `p` is taken verbatim from attacker-controlled input. `caller_principal_id()` is never compared to `p`. [1](#0-0) 

**Confirmation-text validation is text-only, not identity-bound:**

`validate_confirmation_text` in `rs/sns/swap/src/swap.rs` lines 363–384 only checks that the supplied string equals the stored expected string. It does not verify that the entity supplying the text is the same principal whose funds are being committed. [2](#0-1) 

**`confirmation_text` is publicly readable:**

`get_init` is an unauthenticated `#[query]` endpoint. Any caller can retrieve `Init.confirmation_text` before mounting the attack. [3](#0-2) 

**Ticket check does not block the attack:**

Lines 1248–1272 of `swap.rs` include an optional ticket check. The check only rejects the call if `amount_ticket > requested_increment_e8s`. If the victim has no open ticket (the old payment flow does not require one), the check is skipped entirely. Even when a ticket exists, the check passes as long as the ticket amount does not exceed the available subaccount balance, which is the normal case. [4](#0-3) 

**Participation is persisted with no victim-initiated undo path:**

After all checks pass, `self.buyers` is updated at lines 1285–1288. `error_refund_icp` is only available when `refresh_buyer_tokens` itself failed; once the entry is written, the victim's ICP remains locked until the swap closes. [5](#0-4) 

**Exploit flow:**
1. SNS swap deployed with `confirmation_text = "I confirm I am not a US person"`.
2. Victim (`principal V`) transfers ICP to `swap_canister[subaccount(V)]`, intending to review terms before committing.
3. Attacker reads `confirmation_text` via unauthenticated `get_init` query.
4. Attacker submits ingress update from any identity: `refresh_buyer_tokens({ buyer: "<V>", confirmation_text: "I confirm I am not a US person" })`.
5. Canister resolves `p = V`, `validate_confirmation_text` passes (text matches), ledger balance is read from `subaccount(V)`, and `buyers[V]` is written.
6. Victim's ICP is committed. Victim never signed the confirmation text.

## Impact Explanation

The `confirmation_text` mechanism is the SNS creator's explicit tool to obtain regulatory or legal consent (e.g., KYC attestations, jurisdiction exclusions) before allowing participation. This bypass makes it a no-op for any buyer who has already transferred ICP to their subaccount. The victim is forced into a financial position — holding SNS tokens or having ICP locked — under terms they never accepted. This constitutes a **significant SNS security impact with concrete user and protocol harm**, qualifying as **High ($2,000–$10,000)** under the "Significant SNS security impact with concrete user or protocol harm" bounty category.

## Likelihood Explanation

No privileges are required. The `confirmation_text` is publicly readable via an unauthenticated query. The victim's principal is observable on-chain from prior ledger transfers to the swap subaccount. The only precondition is that the victim has transferred ICP to their subaccount, which is the normal first step in the participation flow, making front-running straightforward and repeatable for every affected buyer.

## Recommendation

Enforce caller == buyer when `arg.buyer` is non-empty:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    let p = PrincipalId::from_str(&arg.buyer).unwrap();
    if p != caller_principal_id() {
        panic!("Caller {} is not authorized to refresh tokens on behalf of {}", caller_principal_id(), p);
    }
    p
};
```

Alternatively, remove the `buyer` field entirely and always derive the buyer from `caller_principal_id()`, consistent with how `new_sale_ticket` and `notify_payment_failure` are implemented. [6](#0-5) [7](#0-6) 

## Proof of Concept

A deterministic PocketIC integration test can prove this:

1. Deploy a swap canister with `confirmation_text = "I agree"` and a funded ICP ledger.
2. Transfer ICP from principal `V` to `swap_canister[subaccount(V)]` using `V`'s identity.
3. From a **different** principal `A` (attacker), call `refresh_buyer_tokens({ buyer: V.to_text(), confirmation_text: "I agree" })`.
4. Assert the call succeeds (returns `Ok`).
5. Query `get_buyer_state({ principal_id: V })` and assert `amount_icp_e8s > 0`.
6. Assert that `V` never called `refresh_buyer_tokens` or `new_sale_ticket` — confirming the consent bypass. [8](#0-7)

### Citations

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

**File:** rs/sns/swap/canister/canister.rs (L225-229)
```rust
#[query]
async fn get_open_ticket(request: GetOpenTicketRequest) -> GetOpenTicketResponse {
    log!(INFO, "get_open_ticket");
    swap().get_open_ticket(&request, caller_principal_id())
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

**File:** rs/sns/swap/canister/canister.rs (L252-256)
```rust
#[update]
fn notify_payment_failure(_request: NotifyPaymentFailureRequest) -> NotifyPaymentFailureResponse {
    log!(INFO, "notify_payment_failure");
    swap_mut().notify_payment_failure(&caller_principal_id())
}
```

**File:** rs/sns/swap/src/swap.rs (L363-384)
```rust
        pub fn validate_confirmation_text(
            &self,
            confirmation_text: Option<String>,
        ) -> Result<(), String> {
            match (
                self.init_or_panic().confirmation_text.as_ref(),
                confirmation_text,
            ) {
                (Some(expected_text), Some(text)) => {
                    if &text != expected_text {
                        Err("The value of `confirmation_text` does not match the value provided in SNS init payload.".to_string())
                    } else {
                        Ok(())
                    }
                }
                (Some(_), None) => Err("No value provided for `confirmation_text`.".to_string()),
                (None, Some(_)) => {
                    Err("Found a value for `confirmation_text`, expected none.".to_string())
                }
                (None, None) => Ok(()),
            }
        }
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

**File:** rs/sns/swap/src/swap.rs (L1285-1288)
```rust
        self.buyers
            .entry(buyer.to_string())
            .or_insert_with(|| BuyerState::new(0))
            .set_amount_icp_e8s(new_balance_e8s);
```
