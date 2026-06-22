Let me look at the full `refresh_buyer_token_e8s` function and the `error_refund_icp` to understand the complete impact.

### Title
Unprivileged Caller Can Force-Register Any Principal as a Swap Participant via Unchecked `buyer` Field in `refresh_buyer_tokens` — (`rs/sns/swap/canister/canister.rs`)

---

### Summary

The `refresh_buyer_tokens` ingress endpoint accepts an arbitrary `buyer` principal without verifying it matches the caller. Any unprivileged attacker can deposit ICP into a victim's subaccount of the swap canister and then call `refresh_buyer_tokens` with `buyer` set to the victim's principal, causing the swap canister to credit the victim's `BuyerState` and register them as a participant — without the victim's knowledge or consent.

---

### Finding Description

In `rs/sns/swap/canister/canister.rs` lines 128–143, the `refresh_buyer_tokens` handler resolves the effective buyer principal as follows: [1](#0-0) 

If `arg.buyer` is non-empty, it is parsed directly as a `PrincipalId` with no check that it equals `caller_principal_id()`. The resolved principal `p` is passed verbatim to `refresh_buyer_token_e8s`.

Inside `refresh_buyer_token_e8s` (`rs/sns/swap/src/swap.rs` lines 1152–1163), the function queries the ICP ledger balance for `Account { owner: swap_canister, subaccount: principal_to_subaccount(&buyer) }`: [2](#0-1) 

The ticket system at lines 1248–1272 is **not a mandatory gate** — the comment explicitly states "If there exists no ticket for the buyer, the payment flow will simply ignore the ticket": [3](#0-2) 

If the balance meets `min_participant_icp_e8s`, the victim is inserted into `self.buyers` and registered as a participant: [4](#0-3) 

The proto comment for `RefreshBuyerTokensRequest.buyer` says "If not specified, the caller is used" — implying caller-identity semantics — but no enforcement exists: [5](#0-4) 

---

### Impact Explanation

1. **Forced swap participation without consent.** The victim is registered as a direct participant in a token sale they never chose to enter.

2. **Confirmation text (ToS) accepted on victim's behalf.** If the SNS configured a `confirmation_text` (a legally significant terms-of-service string), the attacker supplies it in the call, causing the swap canister to record that the victim accepted terms they never read. This is a concrete consent-integrity violation.

3. **Victim's participation slot consumed.** The victim's `BuyerState` is credited up to `max_participant_icp_e8s`. If the victim later tries to participate legitimately, their slot is already partially or fully consumed.

4. **Swap outcome manipulation.** By registering many victim principals each with the minimum ICP amount, an attacker can push the swap's `direct_participation_icp_e8s` toward the minimum ICP target, potentially causing the swap to commit when it otherwise would have aborted. The attacker loses the deposited ICP but gains control over the swap's outcome.

5. **If swap commits:** The ICP in the victim's subaccount is swept to SNS governance; the victim receives unwanted SNS neurons they cannot refuse.

6. **If swap aborts:** After `sweep_icp`, the victim can call `error_refund_icp` to recover the ICP to their own account — but the attacker has already lost their ICP and the victim was still involuntarily registered during the open period. [6](#0-5) 

---

### Likelihood Explanation

- The attack requires only an ICP ledger transfer (publicly available) and a single ingress call to `refresh_buyer_tokens` with a non-empty `buyer` field.
- No privileged access, no key material, no governance majority, and no social engineering is required.
- The swap canister is a public ingress endpoint; any principal on the IC can call it.
- The ticket system, which was introduced as a payment-flow guard, explicitly skips validation when no ticket exists for the buyer, leaving no secondary defense.

---

### Recommendation

In `rs/sns/swap/canister/canister.rs`, enforce that the resolved buyer principal equals the caller:

```rust
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let caller = caller_principal_id();
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller
    } else {
        let parsed = PrincipalId::from_str(&arg.buyer).unwrap();
        // Enforce: only the caller may refresh their own participation.
        assert_eq!(parsed, caller, "buyer must match caller");
        parsed
    };
    // ...
}
```

Alternatively, remove the `buyer` field entirely from `RefreshBuyerTokensRequest` and always use the caller, since the field's only documented purpose is "if not specified, the caller is used." [1](#0-0) 

---

### Proof of Concept

```
// Setup: swap is OPEN, victim = Principal V, attacker = Principal A
// min_participant_icp_e8s = 1_000_000 (0.01 ICP)

// Step 1 (attacker): Transfer ICP to victim's subaccount of the swap canister
icp_ledger.transfer({
    to: Account {
        owner: swap_canister_id,
        subaccount: principal_to_subaccount(V),
    },
    amount: 1_000_000,  // meets min_participant_icp_e8s
    from: attacker_account,
})

// Step 2 (attacker): Call refresh_buyer_tokens with buyer = victim
swap_canister.refresh_buyer_tokens({
    buyer: V.to_text(),
    confirmation_text: Some("I confirm"),  // attacker supplies ToS on victim's behalf
})

// Result: swap.buyers[V] is now set with amount_icp_e8s = 1_000_000
// V is a registered participant; V never called anything.
// Assert: get_buyer_state(V).buyer_state.is_some() == true
```

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

**File:** rs/sns/swap/src/swap.rs (L1152-1163)
```rust
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

**File:** rs/sns/swap/src/swap.rs (L1274-1288)
```rust
        // Append to a new buyer to the BUYERS_LIST_INDEX
        let is_preexisting_buyer = self.buyers.contains_key(&buyer.to_string());
        if !is_preexisting_buyer {
            insert_buyer_into_buyers_list_index(buyer)
                .map_err(|grow_failed| {
                    format!(
                        "Failed to add buyer {buyer} to state, the canister's stable memory could not grow: {grow_failed}"
                    )
                })?;
        }

        self.buyers
            .entry(buyer.to_string())
            .or_insert_with(|| BuyerState::new(0))
            .set_amount_icp_e8s(new_balance_e8s);
```

**File:** rs/sns/swap/src/swap.rs (L1906-1924)
```rust
    /// Requests a refund of ICP tokens transferred to the Swap
    /// canister that was either never notified (via the
    /// refresh_buyer_tokens Candid method), or not fully accepted (by
    /// refresh_buyer_tokens).
    ///
    /// This method makes no changes (and instead panics) unless
    /// finalization has completed successfully (see the finalize
    /// method), which can only happen after self has entered the
    /// Aborted or Committed state.
    ///
    /// The entire balance in `subaccount(swap_canister, P)` is
    /// transferred to request.principal_id (minus the transfer fee,
    /// of course).
    ///
    /// This method is secure because it only transfers tokens from a
    /// principal's subaccount (of the Swap canister) to the
    /// principal's own account, i.e., the tokens were held in escrow
    /// for the principal (buyer) before the call and are returned to
    /// the same principal.
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
