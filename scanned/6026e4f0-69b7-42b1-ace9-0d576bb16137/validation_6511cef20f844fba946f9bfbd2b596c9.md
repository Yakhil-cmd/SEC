### Title
Permissionless `refresh_buyer_tokens` Allows Forced Participation and Confirmation-Text Bypass in SNS Swap — (File: rs/sns/swap/canister/canister.rs)

---

### Summary

The SNS Swap canister's `refresh_buyer_tokens` endpoint accepts an arbitrary `buyer` principal supplied by the caller. Because no check is made that the supplied principal matches the actual caller, any unprivileged ingress sender can invoke the function on behalf of any other buyer. This mirrors the original report's root cause: a permissionless state-mutating function that can be weaponised to grief or front-run a legitimate user's operation.

---

### Finding Description

In `rs/sns/swap/canister/canister.rs`, the `refresh_buyer_tokens` update method resolves the target buyer from the caller-supplied `arg.buyer` string rather than from the authenticated caller identity: [1](#0-0) 

```rust
#[update]
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()   // ← any principal accepted
    };
    ...
    swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, this_canister_id(), &icp_ledger)
        .await
```

The `RefreshBuyerTokensRequest.buyer` field is defined as a plain string with no authentication binding: [2](#0-1) 

Inside `refresh_buyer_token_e8s`, the function:

1. Validates the (caller-supplied) confirmation text against the SNS-stored text.
2. Queries the ICP ledger balance of the **victim's** subaccount.
3. Checks the ticket: if `amount_ticket <= requested_increment_e8s`, the ticket is **deleted** and participation is committed. [3](#0-2) 

**Concrete attack flow:**

| Step | Actor | Action |
|------|-------|--------|
| 1 | Victim | Calls `new_sale_ticket` for amount X ICP |
| 2 | Victim | Transfers X ICP to their swap subaccount on the ICP ledger |
| 3 | Griefer | Calls `refresh_buyer_tokens { buyer: "<victim_principal>", confirmation_text: "<public_text>" }` |
| 4 | Swap canister | Reads victim's balance (X), validates confirmation text (passes — text is public), deletes ticket, commits participation |
| 5 | Victim | Calls `refresh_buyer_tokens` themselves — `requested_increment_e8s = X − X = 0 < min_participant_icp_e8s` → **error** |

The griefer can repeat step 3 every time the victim deposits additional ICP, permanently racing ahead of the victim's own calls.

A second vector: if the SNS specifies a `confirmation_text`, the intent is that the **buyer** must affirmatively accept the terms. Because the text is stored in public canister state (readable via `get_init`), the griefer can supply it verbatim, committing the victim's ICP to the swap without the victim ever having agreed. [4](#0-3) 

---

### Impact Explanation

- **Forced participation without consent**: Any buyer who has transferred ICP to their swap subaccount can have their participation committed by a third party. If the swap succeeds, the victim receives SNS tokens they did not explicitly choose to acquire at that moment.
- **Confirmation-text bypass**: The `confirmation_text` guard — designed to prove the buyer read and accepted the swap terms — is rendered ineffective because the griefer, not the buyer, supplies the text.
- **Ticket griefing / front-running**: The victim's open ticket is deleted by the griefer's call. The victim's own subsequent call fails with a "balance already accounted for" error, disrupting the ticket-based payment flow and forcing the victim to restart the participation sequence.
- **Repeated griefing**: A griefer can loop on step 3 each time the victim deposits more ICP, preventing the victim from ever accumulating a larger participation amount in a single atomic step.

---

### Likelihood Explanation

- No special privilege is required: any ingress sender with a non-anonymous principal can call `refresh_buyer_tokens`.
- The victim's principal is observable from public swap state (`list_direct_participants`, `get_buyer_state`).
- The confirmation text is readable from `get_init` / `get_sale_parameters`.
- The attack costs only the ICP ledger query fee (cycles) and is trivially scriptable.

---

### Recommendation

1. **Restrict the `buyer` field to the caller**: if `arg.buyer` is non-empty, assert `PrincipalId::from_str(&arg.buyer) == caller_principal_id()` and reject mismatches. This mirrors the fix recommended in the original report (make the deposit function permissioned to the node-operator manager).
2. **Alternatively**, remove the `buyer` field entirely and always derive the target from `caller_principal_id()`, as `new_sale_ticket` already does.
3. **Add a minimum-increment guard** analogous to the "minimum ETH deposit" recommendation: reject calls where the ledger balance has not increased since the last recorded participation, preventing zero-increment state churn.

---

### Proof of Concept

```
# Step 1 – victim creates ticket
dfx canister call sns_swap new_sale_ticket '(

### Citations

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

**File:** rs/sns/swap/src/swap.rs (L1149-1151)
```rust
        // User input validation doesn't expire after await, so this check doesn't need repetition.
        self.validate_confirmation_text(confirmation_text)?;

```

**File:** rs/sns/swap/src/swap.rs (L1248-1271)
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
```
