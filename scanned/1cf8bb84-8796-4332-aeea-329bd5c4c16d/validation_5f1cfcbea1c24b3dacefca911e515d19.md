### Title
Unprivileged Caller Can Register Participation for Arbitrary Buyer in SNS Swap, Bypassing Confirmation-Text Consent — (File: rs/sns/swap/canister/canister.rs)

---

### Summary
The `refresh_buyer_tokens` endpoint in the SNS Swap canister accepts a caller-supplied `buyer` principal without verifying it matches the actual caller. Any unprivileged ingress sender can trigger participation registration — including ticket deletion and confirmation-text acceptance — on behalf of any other user, directly analogous to the `LockedStakingPools` front-running pattern where a third party manipulates another user's position state.

---

### Finding Description

In `rs/sns/swap/canister/canister.rs` the public `#[update]` handler resolves the effective buyer as follows:

```rust
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()   // ← arbitrary principal, no caller check
    };
    ...
    swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, this_canister_id(), &icp_ledger)
        .await
``` [1](#0-0) 

The `buyer` field in `RefreshBuyerTokensRequest` is documented as "if not specified, the caller is used," but when it is specified there is no enforcement that it equals the caller. [2](#0-1) 

Inside `refresh_buyer_token_e8s`, the following state mutations are performed under the resolved `buyer` identity — not the actual caller:

1. **Confirmation-text validation** — the SNS-creator-set consent string is checked against the caller-supplied value before the first `await`. Because the confirmation text is stored in public canister state, any attacker can read and replay it. [3](#0-2) 

2. **Open-ticket deletion** — if the victim's subaccount balance satisfies `amount_ticket <= requested_increment_e8s`, the victim's open ticket is irrevocably removed from `OPEN_TICKETS_MEMORY`. [4](#0-3) 

3. **Buyer-state insertion** — the victim is inserted into `self.buyers` and `BUYERS_LIST_INDEX`, consuming one of the finite `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS` slots. [5](#0-4) 

The participant-slot guard checks `(num_direct_participants + 1) * num_sns_neurons_per_basket > MAX_NEURONS_FOR_DIRECT_PARTICIPANTS` only for *new* buyers, so an attacker who registers many victims prematurely can exhaust available slots. [6](#0-5) 

---

### Impact Explanation

| Impact | Detail |
|---|---|
| **Confirmation-text consent bypass** | SNS creators use `confirmation_text` to require explicit user agreement before participation. An attacker who supplies the correct (publicly readable) text on behalf of a victim registers that victim's participation without their knowledge, violating the consent mechanism. |
| **Premature ticket deletion** | A victim's open ticket is deleted when the attacker's call succeeds. The victim loses the ability to manage or cancel their pending participation via the ticket API. |
| **Participant-slot exhaustion** | By calling `refresh_buyer_tokens` for many principals that already hold ≥ `min_participant_icp_e8s` in their subaccounts, an attacker can fill `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS`, blocking all subsequent new participants. |

---

### Likelihood Explanation

**Medium.** The precondition is that the victim has already transferred ICP to their swap-canister subaccount (a normal step in the participation flow). Once that transfer exists on the ICP ledger, any unprivileged ingress sender can call `refresh_buyer_tokens` with `buyer = <victim>` at any time. The confirmation text is publicly readable from canister state, so no privileged information is required. The attack is cheap (one ingress message per victim) and requires no coordination with the victim.

---

### Recommendation

Enforce that the resolved buyer equals the actual caller. Replace the unchecked branch with:

```rust
let p: PrincipalId = caller_principal_id();
// Optionally allow an explicit buyer only if it equals the caller,
// to preserve backward-compatible tooling:
if !arg.buyer.is_empty() {
    let requested = PrincipalId::from_str(&arg.buyer).unwrap();
    if requested != p {
        panic!("buyer must equal the caller");
    }
}
```

This mirrors the recommendation in the original report: "allow to participate using only the `msg.sender` address as a recipient."

---

### Proof of Concept

**Setup**: SNS swap is `Open`; swap requires `confirmation_text = "I agree"` (readable from `get_state`); `min_participant_icp_e8s = 2_0000_0000`.

1. **Alice** calls `new_sale_ticket` → ticket created for `10_0000_0000` e8s.
2. **Alice** calls `icrc1_transfer` → transfers `10_0000_0000` e8s to `swap_canister[subaccount = principal_to_subaccount(Alice)]`.
3. **Attacker Bob** (any principal) submits ingress:
   ```
   refresh_buyer_tokens({
     buyer: "<Alice's principal text>",
     confirmation_text: Some("I agree")
   })
   ```
4. Inside `refresh_buyer_token_e8s`:
   - `validate_confirmation_text("I agree")` → passes (correct text).
   - `account_balance(swap_canister[Alice's subaccount])` → `10_0000_0000`.
   - `amount_ticket (10e8) <= requested_increment (10e8)` → ticket **deleted**.
   - Alice inserted into `buyers` with `10_0000_0000` e8s.
5. **Alice** never explicitly confirmed participation; her ICP is now locked in the swap and her ticket is gone.
6. If the swap is near `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS`, Bob repeats step 3 for many other principals that have pending subaccount balances, exhausting available slots and blocking new participants. [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

**File:** rs/sns/swap/canister/canister.rs (L126-143)
```rust
/// See `Swap.refresh_buyer_token_e8`.
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

**File:** rs/sns/swap/src/swap.rs (L1180-1198)
```rust
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
        }
```

**File:** rs/sns/swap/src/swap.rs (L1248-1291)
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
        // We compute the current participation amounts once and store the result in Swap's state,
        // for efficiency reasons.
        self.update_total_participation_amounts();
```
