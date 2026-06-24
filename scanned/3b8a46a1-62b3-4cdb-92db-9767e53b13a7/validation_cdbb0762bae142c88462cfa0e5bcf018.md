### Title
Arbitrary Caller Can Force Victim's ICP Participation in SNS Swap Without Consent — (File: `rs/sns/swap/canister/canister.rs`)

---

### Summary

The `refresh_buyer_tokens` update endpoint in the SNS Swap canister accepts an arbitrary `buyer` principal string in its request without verifying that the actual `caller()` is that buyer. Any unprivileged ingress sender can force any victim's ICP (already deposited to the swap canister's subaccount) into the swap, locking it until finalization, bypassing the victim's explicit consent and the SNS-configured confirmation text requirement.

---

### Finding Description

In `rs/sns/swap/canister/canister.rs`, the `refresh_buyer_tokens` update function accepts a `RefreshBuyerTokensRequest` with a `buyer` field (a string). When non-empty, the function parses it as a `PrincipalId` and uses it as the buyer — with **no check that `caller() == buyer`**: [1](#0-0) 

```rust
#[update]
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()  // ← attacker-controlled, no caller check
    };
    ...
    swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, this_canister_id(), &icp_ledger)
        .await
}
```

The proto definition confirms `buyer` is optional and defaults to the caller only as a convention, not an enforcement: [2](#0-1) 

The `refresh_buyer_token_e8s` implementation then:

1. Queries the ICP balance of `swap_canister[subaccount(buyer)]` on the ICP ledger
2. Validates the caller-supplied `confirmation_text` against the SNS-configured text
3. Credits the balance to `buyer` in the swap's `buyers` map
4. **Deletes the buyer's open ticket** from stable memory [3](#0-2) [4](#0-3) [5](#0-4) 

The `RefreshBuyerTokensRequest.buyer` field is documented as "If not specified, the caller is used," implying the intent is for the caller to be the buyer. When specified, there is no enforcement of this invariant. [6](#0-5) 

---

### Impact Explanation

An attacker can:

1. Observe that a victim has sent ICP to `swap_canister[subaccount(victim)]` (visible on the public ICP ledger).
2. Call `refresh_buyer_tokens({ buyer: victim_principal, confirmation_text: "correct text" })`. The confirmation text is publicly visible from SNS initialization parameters.
3. The victim's ICP is now registered as their participation in the swap — **without the victim's consent**.
4. The victim's ICP is **locked until the swap finalizes** (committed or aborted). During the `Open` lifecycle, the victim cannot reclaim their ICP.
5. The victim's open ticket is deleted, disrupting the intended payment flow.

The confirmation text requirement — designed to ensure informed, explicit consent — is completely bypassed. If the victim sent ICP by mistake or changed their mind, they are forced into the swap. If the swap commits, the victim receives SNS tokens they did not want; if it aborts, they eventually get their ICP back minus fees, but only after the swap closes.

---

### Likelihood Explanation

**High.** The `refresh_buyer_tokens` endpoint is publicly callable by any unprivileged ingress sender with no authentication beyond a valid principal. The victim's ICP deposit is visible on the public ICP ledger. The confirmation text is publicly visible from the SNS initialization state. No special access, admin keys, or privileged roles are required. The attack requires only knowledge of the victim's principal and the swap's confirmation text.

---

### Recommendation

Enforce that when `buyer` is specified in the request, it must equal `caller()`. The simplest fix is to always derive the buyer from `caller()` and ignore the `buyer` field entirely, or reject requests where `buyer != caller()`:

```rust
let p: PrincipalId = caller_principal_id();
// Ignore arg.buyer entirely, or assert arg.buyer == caller
```

This mirrors the fix recommended in the DaosLocker report: use `msg.sender` instead of accepting an arbitrary input for the entity address.

---

### Proof of Concept

1. Alice sends 10 ICP to `swap_canister[subaccount(alice_principal)]` on the ICP ledger, intending to participate later.
2. Alice changes her mind (e.g., swap terms changed, or she sent ICP by mistake).
3. Before Alice can wait for the swap to finalize and call `error_refund_icp`, Bob (attacker) calls:
   ```
   refresh_buyer_tokens({
     buyer: "alice_principal_text",
     confirmation_text: Some("I confirm I want to participate in this SNS swap")
   })
   ```
4. The swap canister queries `swap_canister[subaccount(alice)]`, finds 10 ICP, and registers Alice as a buyer.
5. Alice's open ticket (if any) is deleted from stable memory.
6. Alice's 10 ICP is now locked in the swap. She cannot reclaim it until the swap finalizes.
7. If the swap commits, Alice receives unwanted SNS tokens. If it aborts, she gets her ICP back minus the ICP ledger transfer fee — but only after the swap closes, which could be days or weeks.

This is directly analogous to the DaosLocker `collect(address dao)` vulnerability: an arbitrary entity identifier is accepted as input, privileged operations are performed on behalf of that entity (registering participation, deleting tickets, accepting confirmation text), and the actual caller is never verified to be that entity. [1](#0-0) [7](#0-6) [4](#0-3)

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

**File:** rs/sns/swap/src/swap.rs (L1249-1271)
```rust
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
