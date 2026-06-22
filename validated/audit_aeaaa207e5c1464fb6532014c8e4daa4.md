### Title
Unauthenticated `buyer` Field in `refresh_buyer_tokens` Allows Forced Participation and Confirmation-Text Consent Bypass in SNS Swap — (File: `rs/sns/swap/canister/canister.rs`)

---

### Summary

The SNS Swap canister's `refresh_buyer_tokens` endpoint accepts an arbitrary `buyer` principal supplied by any unprivileged ingress caller. Because the ICP deposit and the participation-registration call are two separate, non-atomic steps, any third party can call `refresh_buyer_tokens(buyer=<victim>, confirmation_text=<public_text>)` the moment a victim's ICP lands in the swap subaccount, registering the victim's participation without their explicit consent and bypassing the confirmation-text mechanism. If the swap succeeds and SNS tokens are worth less than the deposited ICP, the victim suffers a real economic loss.

---

### Finding Description

The SNS Swap canister uses a two-step, non-atomic deposit flow that is structurally identical to the vault pattern described in the external report:

**Step 1 – Transfer (separate transaction)**
The buyer sends ICP to the swap canister's per-buyer subaccount on the ICP ledger:
`swap_canister [ principal_to_subaccount(buyer) ]`

**Step 2 – Registration (separate transaction)**
The buyer (or *anyone*) calls `refresh_buyer_tokens`, which queries the ledger for the subaccount balance and records the participation.

The critical flaw is in `canister.rs`:

```rust
// rs/sns/swap/canister/canister.rs  lines 128-143
#[update]
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()   // ← any caller, any principal
    };
    ...
    swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, ...)
        .await
``` [1](#0-0) 

There is **no check** that the caller equals `p`. Any ingress sender can supply an arbitrary `buyer` string.

Inside `refresh_buyer_token_e8s`, the confirmation text is validated against the text provided by the **caller**, not the buyer:

```rust
// rs/sns/swap/src/swap.rs  lines 1149-1150
// User input validation doesn't expire after await, so this check doesn't need repetition.
self.validate_confirmation_text(confirmation_text)?;
``` [2](#0-1) 

The confirmation text is a public value set at SNS initialization time; any attacker can read it and supply it verbatim. After the async ledger balance query, the function unconditionally records the victim's participation:

```rust
// rs/sns/swap/src/swap.rs  lines 1285-1291
self.buyers
    .entry(buyer.to_string())
    .or_insert_with(|| BuyerState::new(0))
    .set_amount_icp_e8s(new_balance_e8s);
self.update_total_participation_amounts();
``` [3](#0-2) 

The proto definition confirms the field is intentionally open to third-party callers:

```proto
// rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto  lines 843-845
message RefreshBuyerTokensRequest {
  // If not specified, the caller is used.
  string buyer = 1;
``` [4](#0-3) 

---

### Impact Explanation

1. **Confirmation-text consent bypass.** The confirmation text exists precisely to record the buyer's explicit agreement to the swap terms. Because any caller can supply it on behalf of any victim, the mechanism is rendered ineffective. A victim who transferred ICP but had not yet decided to confirm is forced into participation.

2. **Forced participation / economic loss.** Once `refresh_buyer_tokens` is called for a victim, their ICP is locked as a registered participation. If the swap commits and the SNS token price at settlement is below the ICP price at deposit time, the victim suffers a direct economic loss — their ICP was converted to lower-value SNS tokens without their consent.

3. **Ticket-system bypass.** The ticket guard only fires when an open ticket exists for the buyer. If the victim has no open ticket (the common case for a first-time participant), the ticket check is silently skipped and the attack proceeds unimpeded. [5](#0-4) 

---

### Likelihood Explanation

- **Reachable entry path:** Any unprivileged ingress sender can call `refresh_buyer_tokens` with an arbitrary `buyer` field. No special role, key, or governance majority is required.
- **Observable trigger:** ICP ledger transfers to swap subaccounts are public. An attacker monitoring the ledger can detect a victim's deposit and immediately submit the registration call.
- **Confirmation text is public:** It is stored in the swap's `Init` struct and returned by `get_sale_parameters`, so the attacker can always supply the correct text.
- **Window of opportunity:** The window between the victim's ICP transfer and their own `refresh_buyer_tokens` call is non-zero and practically exploitable, especially for users relying on slow frontends or manual workflows.

---

### Recommendation

1. **Enforce caller == buyer.** In `canister.rs`, remove the branch that accepts a non-empty `buyer` field from an arbitrary caller, or add an explicit check:
   ```rust
   if !arg.buyer.is_empty() && p != caller_principal_id() {
       panic!("caller must match buyer");
   }
   ```
2. **If third-party registration must be supported**, require the buyer to have pre-authorized the caller (e.g., via a signed intent stored on-chain), so the confirmation-text consent is tied to the buyer's identity, not the caller's.
3. **Treat the confirmation text as buyer-bound**, not caller-bound: only accept it when the caller is the buyer themselves.

---

### Proof of Concept

1. Alice transfers 100 ICP to `swap_canister[principal_to_subaccount(Alice)]` on the ICP ledger.
2. Eve reads the swap's public `confirmation_text` via `get_sale_parameters`.
3. Eve submits an ingress message:
   ```
   refresh_buyer_tokens({
     buyer: "<Alice's principal

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

**File:** rs/sns/swap/src/swap.rs (L1149-1150)
```rust
        // User input validation doesn't expire after await, so this check doesn't need repetition.
        self.validate_confirmation_text(confirmation_text)?;
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

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L843-845)
```text
message RefreshBuyerTokensRequest {
  // If not specified, the caller is used.
  string buyer = 1;
```
