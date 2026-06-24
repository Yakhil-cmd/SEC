### Title
Caller Not Validated Against `buyer` in SNS Swap `refresh_buyer_tokens`, Bypassing Confirmation-Text Consent - (File: rs/sns/swap/canister/canister.rs)

---

### Summary

The SNS Swap canister's `refresh_buyer_tokens` endpoint accepts an arbitrary `buyer` principal in its request payload and uses it directly without verifying that the caller matches the specified buyer. Any unprivileged ingress sender can therefore trigger participation registration on behalf of any other principal, bypassing the SNS-configured confirmation-text consent mechanism.

---

### Finding Description

`refresh_buyer_tokens` in `rs/sns/swap/canister/canister.rs` resolves the effective buyer principal as follows:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    PrincipalId::from_str(&arg.buyer).unwrap()  // no caller == buyer check
};
``` [1](#0-0) 

When `arg.buyer` is non-empty, the caller's identity is completely ignored. The resolved principal `p` is then passed to `refresh_buyer_token_e8s`, which:

1. Queries the ICP ledger balance of `buyer`'s subaccount on the swap canister.
2. Validates the optional `confirmation_text` against the SNS-configured consent string.
3. Registers the buyer's participation and updates `self.buyers`. [2](#0-1) 

The `confirmation_text` field is explicitly designed as a consent gate — the SNS operator sets a terms-of-service string during initialization, and participants must echo it to register:

```
// To accept the swap participation confirmation, a participant should send
// the confirmation text via refresh_buyer_tokens, matching the text set
// during SNS initialization.
``` [3](#0-2) 

Because the confirmation text is a public, on-chain string, any third party can read it and supply it in a call with `buyer = <victim>`, registering the victim's participation without the victim ever having called `refresh_buyer_tokens` themselves.

The two-step flow is:

1. Alice transfers ICP to her subaccount on the swap canister (public ledger event).
2. Eve observes the transfer, reads the public confirmation text, and calls `refresh_buyer_tokens(buyer = Alice, confirmation_text = <text>)`.
3. Alice's participation is registered — Alice never explicitly agreed to the confirmation text. [4](#0-3) 

This is structurally identical to the ENS report: the "commit" step (ICP transfer to a subaccount) is not bound to the caller of the "register" step (`refresh_buyer_tokens`), so a third party can repurpose the commit.

---

### Impact Explanation

- **Confirmation-text consent bypass**: The confirmation text is the only mechanism by which a participant explicitly agrees to SNS-specific terms (e.g., legal disclaimers, tokenomics disclosures). Eve can satisfy this check on Alice's behalf, registering Alice's participation without Alice's explicit agreement.
- **Forced participation**: If Alice transferred ICP intending to participate but then changed her mind before calling `refresh_buyer_tokens`, Eve can force-register Alice's participation. Alice cannot reclaim her ICP until the swap closes (`error_refund_icp` is only available post-close).
- **Participant-slot exhaustion**: An attacker can call `refresh_buyer_tokens` for many principals who have deposited ICP, consuming the `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS` cap and blocking new legitimate participants. [5](#0-4) 

---

### Likelihood Explanation

- All ICP ledger transfers are public; an attacker can monitor the ledger for transfers to swap subaccounts.
- The confirmation text is a public on-chain string readable by anyone.
- No special privilege, key, or majority is required — a single unprivileged ingress call suffices.
- The attack is most impactful for SNS launches that set a non-empty `confirmation_text`, which is a documented and supported feature.

---

### Recommendation

Validate that the caller matches the specified buyer when `arg.buyer` is non-empty:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    let specified = PrincipalId::from_str(&arg.buyer).unwrap();
    if specified != caller_principal_id() {
        panic!("caller must match the specified buyer");
    }
    specified
};
```

Alternatively, remove the `buyer` override field entirely and always use `caller_principal_id()`, since the ICP subaccount is already derived from the buyer's principal and there is no legitimate use case for a third party to register participation on behalf of another. [6](#0-5) 

---

### Proof of Concept

1. An SNS swap is open with `confirmation_text = "I agree to the terms"`.
2. Alice transfers 10 ICP to `swap_canister_subaccount(Alice)` on the ICP ledger.
3. Eve observes the transfer on the public ledger.
4. Eve calls (as any principal):
   ```
   refresh_buyer_tokens({
     buyer: "<Alice's principal>",
     confirmation_text: Some("I agree to the terms")
   })
   ```
5. The swap canister queries Alice's subaccount balance (10 ICP), validates the confirmation text (passes), and registers Alice as a participant — without Alice ever having called `refresh_buyer_tokens`.
6. Alice's ICP is now committed to the swap. She cannot reclaim it until the swap closes. [7](#0-6)

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

**File:** rs/sns/swap/src/swap.rs (L1179-1197)
```rust
        // Check that the maximum number of participants has not been reached yet.
        {
            let num_direct_participants = self.buyers.len() as u64;
            let num_sns_neurons_per_basket = params
                .neuron_basket_construction_parameters
                .as_ref()
                .expect("neuron_basket_construction_parameters must be specified")
                .count;
            if (num_direct_participants + 1) * num_sns_neurons_per_basket
                > MAX_NEURONS_FOR_DIRECT_PARTICIPANTS
            {
                return Err(format!(
                    "The swap has reached the maximum number of direct participants ({num_direct_participants}) and does \
                     not accept new participants; existing participants may still increase their \
                     ICP participation amount. This constraint ensures that SNS neuron baskets can \
                     be created for all existing participants (SNS neuron basket size: {num_sns_neurons_per_basket}, \
                     MAX_NEURONS_FOR_DIRECT_PARTICIPANTS: {MAX_NEURONS_FOR_DIRECT_PARTICIPANTS}).",
                ));
            }
```

**File:** rs/sns/swap/src/swap.rs (L1248-1288)
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

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L843-851)
```text
message RefreshBuyerTokensRequest {
  // If not specified, the caller is used.
  string buyer = 1;

  // To accept the swap participation confirmation, a participant should send
  // the confirmation text via refresh_buyer_tokens, matching the text set
  // during SNS initialization.
  optional string confirmation_text = 2;
}
```
